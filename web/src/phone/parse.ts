/* Команды телефону в реплике: «поставь будильник на 8:00», «таймер на 10
   минут», «покажи будильники», «открой Telegram», «включи фонарик» — и
   по-английски. Только разбор, без исполнения: исполняет actions.ts через
   плагин PhoneActions. Модуль самодостаточный (без импортов) —
   scripts/test_phone_actions.py гоняет его под Node.

   Команда — только повелительная («поставь», «открой») в начале реплики или
   после «можешь / надо / хочу»: «ты поставила будильник?» — разговор, не
   команда. «На компьютере» — не телефону: такая реплика уходит как обычно. */

const W = '[\\p{L}\\p{N}_]';
const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;

/** Шаблон с юникодными \b (в JS \b — только латиница). */
function rx(src: string, flags = 'iu'): RegExp {
  return new RegExp(src.replace(/\\b/g, B), flags);
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// ─── Подготовка реплики ───

export function normalize(text: string): string {
  return text.toLowerCase().replace(/ё/g, 'е').replace(/[’`]/g, "'").replace(/\s+/g, ' ').trim();
}

const POLITE_RE = rx(String.raw`[,\s]*\b(?:пожалуйста|плиз|please|pls)\b[,!.\s]*`, 'giu');
const LEAD_RE = rx(String.raw`^(?:(?:а|ну|и|так|слушай|давай|ок|окей|ok|okay|hey|эй|so)\b[,!\s]+)+`);

/** Реплика без обращения, «пожалуйста» и частиц в начале, нижний регистр. */
function prepare(text: string, names: string[]): string {
  let t = normalize(text);
  const alt = names.map((n) => normalize(n)).filter(Boolean).map(esc).join('|');
  if (alt) t = t.replace(rx(String.raw`^(?:${alt})\b[,!:\s]*`), '');
  t = t.replace(POLITE_RE, ' ').replace(/\s+/g, ' ').trim();
  t = t.replace(LEAD_RE, '');
  return t.replace(/[.!…]+$/u, '').trim();
}

const PC_TARGET_RE = rx(
  String.raw`\bна\s+(?:компьютере|компе|ноутбуке|ноуте|пк|маке|макбуке|ядре)\b|` +
    String.raw`\bon\s+(?:the\s+|my\s+)?(?:computer|laptop|pc|mac|desktop)\b`,
);
const NEGATION_RE = rx(
  String.raw`(?:^|\s)не\s+(?:надо|нужно|ставь|заводи|устанавливай|открывай|запускай|включай|выключай|буди)\b|` +
    String.raw`\bdon'?t\b|\bdo\s+not\b|\bnever\s*mind\b`,
);
const PHONE_WORDS_RE = rx(String.raw`\s*\b(?:на|в)\s+(?:телефоне|смартфоне|мобильнике)\b|\s*\bon\s+(?:the\s+|my\s+)?phone\b`, 'giu');

// «можешь поставить», «не могла бы ты открыть», «can you set», «I want to open»
const MODAL =
  String.raw`(?:(?:ты\s+)?(?:можешь|можете|сможешь|сможете)\s+|(?:ты\s+)?(?:не\s+)?мог(?:ла|ли)?\s+бы(?:\s+ты)?\s+|` +
  String.raw`(?:можно|надо|нужно|хочу)\s+|(?:can|could|would|will)\s+you\s+|i\s+(?:want|need)\s+(?:you\s+)?to\s+|` +
  String.raw`i'd\s+like\s+(?:you\s+)?to\s+|let's\s+|go\s+ahead\s+and\s+)?(?:мне\s+|нам\s+)?`;

// ─── Числа словами ───

const RU_NUM: Record<string, number> = {
  ноль: 0, один: 1, одна: 1, одну: 1, два: 2, две: 2, три: 3, четыре: 4, пять: 5, шесть: 6, семь: 7,
  восемь: 8, девять: 9, десять: 10, одиннадцать: 11, двенадцать: 12, тринадцать: 13, четырнадцать: 14,
  пятнадцать: 15, шестнадцать: 16, семнадцать: 17, восемнадцать: 18, девятнадцать: 19, двадцать: 20,
  тридцать: 30, сорок: 40, пятьдесят: 50,
};
const EN_NUM: Record<string, number> = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
  eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17,
  eighteen: 18, nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50,
};
const NUM_WORDS: Record<string, number> = { ...RU_NUM, ...EN_NUM };
const NUM_WORD = `(?:${Object.keys(NUM_WORDS).sort((a, b) => b.length - a.length).join('|')})`;
// «двадцать пять», «forty five»
const NUM_PHRASE = String.raw`(?:${NUM_WORD}(?:[\s-]+${NUM_WORD})?)`;

const has = (o: Record<string, number>, k: string) => Object.prototype.hasOwnProperty.call(o, k);

/** «двадцать пять» → 25, «7» → 7; не число — null. */
function numberOf(s: string): number | null {
  const t = s.trim();
  if (/^\d+$/.test(t)) return parseInt(t, 10);
  let sum = 0;
  const parts = t.split(/[\s-]+/).filter(Boolean);
  if (!parts.length) return null;
  for (const p of parts) {
    if (!has(NUM_WORDS, p)) return null;
    sum += NUM_WORDS[p];
  }
  return sum;
}

// «половину восьмого», «четверть седьмого»: час по порядковому в род. падеже
const ORD_GEN: Record<string, number> = {
  первого: 1, второго: 2, третьего: 3, четвертого: 4, пятого: 5, шестого: 6, седьмого: 7, восьмого: 8,
  девятого: 9, десятого: 10, одиннадцатого: 11, двенадцатого: 12,
};
const ORD = `(${Object.keys(ORD_GEN).join('|')})`;
// «без четверти восемь», «без пяти семь»
const BEFORE_MIN: Record<string, number> = { четверти: 15, пяти: 5, десяти: 10, пятнадцати: 15, двадцати: 20, 'двадцати пяти': 25 };

// ─── Время суток ───

export interface ClockTime {
  hour: number;
  minute: number;
}

type DayPart = 'am' | 'day' | 'evening' | 'night';
const DAYPART_RE = rx(String.raw`\b(утра|дня|вечера|ночи|a\.?m\.?|p\.?m\.?)(?![\p{L}])`);

function dayPartOf(t: string): DayPart | null {
  const m = DAYPART_RE.exec(t);
  if (!m) return null;
  const w = m[1].replace(/\./g, '');
  if (w === 'утра' || w === 'am') return 'am';
  if (w === 'дня') return 'day';
  if (w === 'вечера' || w === 'pm') return 'evening';
  return 'night';
}

// Как adjustHourByDaypart напоминаний (offline/remparse.ts)
function applyDayPart(hour: number, kind: DayPart | null): number {
  const h = ((hour % 24) + 24) % 24;
  if (!kind) return h;
  if (kind === 'am') return h === 12 ? 0 : h;
  if (kind === 'day' || kind === 'evening') return h < 12 ? h + 12 : h;
  if (h === 12) return 0;
  return h >= 6 && h < 12 ? h + 12 : h;
}

const ok = (hour: number, minute: number): ClockTime | null =>
  hour >= 0 && hour <= 24 && minute >= 0 && minute < 60 ? { hour: hour % 24, minute } : null;

// Число, за которым единица длительности, — не время суток («на 10 минут»)
const NOT_DURATION = String.raw`(?!\s*(?:мин|сек|дн|дня|дней|недел|minute|min|sec|second|day|week))`;
const HM_RE = rx(String.raw`(?<![\d.:])(\d{1,2})[:.](\d{2})(?![\d.:])`);
const HALF_RE = rx(String.raw`\b(?:пол\s*-?\s*|половин[уае]\s+)${ORD}\b`);
const QUARTER_RE = rx(String.raw`\bчетверть\s+${ORD}\b`);
const BEFORE_RE = rx(
  String.raw`\bбез\s+(четверти|двадцати\s+пяти|пяти|десяти|пятнадцати|двадцати|\d{1,2})(?:\s+минут\S*)?\s+(${NUM_PHRASE}|\d{1,2})\b`,
);
const EN_PAST_RE = rx(String.raw`\b(half|quarter|${NUM_PHRASE}|\d{1,2})\s+(past|to)\s+(${NUM_PHRASE}|\d{1,2})\b`);
const OCLOCK_RE = rx(String.raw`\b(${NUM_PHRASE}|\d{1,2})\s*o'?clock\b`);
const AMPM_RE = rx(String.raw`(?<![\d:.])(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![\p{L}])`);
const PREP_HOUR_RE = rx(
  String.raw`(?:^|\s)(?:на|в|во|к|at|for|to)\s+(\d{1,2})(?!\d)${NOT_DURATION}(?:\s*(?:час\S*|ч)\b)?` +
    String.raw`(?:\s+(\d{1,2})(?!\d)(?:\s*(?:минут\S*|мин)\b)?)?`,
);
const PREP_WORD_RE = rx(
  String.raw`(?:^|\s)(?:на|в|во|к|at|for|to)\s+(${NUM_WORD}(?:[\s-]+${NUM_WORD}){0,3})\b${NOT_DURATION}`,
);
// «в час дня»
const ONE_OCLOCK_RE = rx(String.raw`(?:^|\s)(?:на|в|к)\s+час\b(?!\s*(?:назад|спустя))`);

/** «двадцать три» → 23; составное — только десятки + единицы. */
function composeNumber(words: string[]): number | null {
  if (words.length === 1) return has(NUM_WORDS, words[0]) ? NUM_WORDS[words[0]] : null;
  if (words.length !== 2 || !has(NUM_WORDS, words[0]) || !has(NUM_WORDS, words[1])) return null;
  const [a, b] = [NUM_WORDS[words[0]], NUM_WORDS[words[1]]];
  return [20, 30, 40, 50].includes(a) && b >= 1 && b <= 9 ? a + b : null;
}

/** «семь тридцать», «двадцать один сорок пять» → часы и минуты. */
function wordsToClock(phrase: string): ClockTime | null {
  const words = phrase.split(/[\s-]+/).filter(Boolean);
  for (const k of [2, 1]) {
    if (words.length < k) continue;
    const h = composeNumber(words.slice(0, k));
    const rest = words.slice(k);
    const m = rest.length ? composeNumber(rest) : 0;
    if (h !== null && m !== null && h <= 24 && m < 60) return { hour: h % 24, minute: m };
  }
  return null;
}
const NOON_RE = rx(String.raw`\b(?:полдень|полудню|noon)\b`);
const MIDNIGHT_RE = rx(String.raw`\b(?:полночь|полуночи|midnight)\b`);

/** Время суток во фразе; bare — ответ на «на сколько?»: «7», «семь», «7 утра». */
export function parseClock(text: string, bare = false): ClockTime | null {
  const t = normalize(text);
  const part = dayPartOf(t);
  if (NOON_RE.test(t)) return { hour: 12, minute: 0 };
  if (MIDNIGHT_RE.test(t)) return { hour: 0, minute: 0 };
  let m = HM_RE.exec(t);
  if (m) {
    const r = ok(parseInt(m[1], 10), parseInt(m[2], 10));
    return r && { hour: applyDayPart(r.hour, part), minute: r.minute };
  }
  m = HALF_RE.exec(t) ?? QUARTER_RE.exec(t);
  if (m) {
    const prev = ORD_GEN[m[1]] - 1;
    const minute = t.slice(m.index).startsWith('четверть') ? 15 : 30;
    return { hour: applyDayPart(prev === 0 ? (part === 'night' ? 0 : 12) : prev, part), minute };
  }
  m = BEFORE_RE.exec(t);
  if (m) {
    const before = BEFORE_MIN[m[1].replace(/\s+/g, ' ')] ?? parseInt(m[1], 10);
    const h = numberOf(m[2]);
    if (h !== null && before > 0 && before < 60 && h >= 1 && h <= 24) {
      return { hour: applyDayPart(h - 1 === 0 ? 12 : h - 1, part), minute: 60 - before };
    }
  }
  m = EN_PAST_RE.exec(t);
  if (m) {
    const mins = m[1] === 'half' ? 30 : m[1] === 'quarter' ? 15 : numberOf(m[1]);
    const h = numberOf(m[3]);
    if (mins !== null && h !== null && mins > 0 && mins < 60 && h >= 0 && h <= 24) {
      if (m[2] === 'past') return { hour: applyDayPart(h, part), minute: mins };
      return { hour: applyDayPart(h - 1 < 0 ? 23 : h - 1 === 0 ? 12 : h - 1, part), minute: 60 - mins };
    }
  }
  m = OCLOCK_RE.exec(t);
  if (m) {
    const h = numberOf(m[1]);
    if (h !== null && h <= 24) return { hour: applyDayPart(h, part), minute: 0 };
  }
  m = AMPM_RE.exec(t);
  if (m) {
    const h = parseInt(m[1], 10);
    if (h <= 12) return { hour: applyDayPart(h, part), minute: 0 };
  }
  m = PREP_HOUR_RE.exec(t);
  if (m) {
    const r = ok(parseInt(m[1], 10), m[2] ? parseInt(m[2], 10) : 0);
    if (r) return { hour: applyDayPart(r.hour, part), minute: r.minute };
  }
  m = PREP_WORD_RE.exec(t);
  if (m) {
    const r = wordsToClock(m[1]);
    if (r) return { hour: applyDayPart(r.hour, part), minute: r.minute };
  }
  if (ONE_OCLOCK_RE.test(t)) return { hour: applyDayPart(1, part), minute: 0 };
  if (bare) {
    const core = t.replace(DAYPART_RE, '').replace(/^(?:на|в|во|к|at|for)\s+/, '').replace(/\s*(?:час\S*)$/, '').trim();
    const r = /^\d{1,2}$/.test(core) ? ok(parseInt(core, 10), 0) : wordsToClock(core);
    if (r) return { hour: applyDayPart(r.hour, part), minute: r.minute };
  }
  return null;
}

// ─── Длительность: «10 минут», «полтора часа», «1 час 30 минут», «half an hour» ───

const DURATION_PHRASES: [RegExp, string][] = [
  [rx(String.raw`\bполчаса\b`, 'giu'), ' 30 минут '],
  [rx(String.raw`\bполминуты\b`, 'giu'), ' 30 секунд '],
  [rx(String.raw`\bчас\s+с\s+половиной\b`, 'giu'), ' 90 минут '],
  [rx(String.raw`\bполтора\s+часа\b`, 'giu'), ' 90 минут '],
  [rx(String.raw`\bполторы\s+минуты\b`, 'giu'), ' 90 секунд '],
  [rx(String.raw`\ban\s+hour\s+and\s+a\s+half\b`, 'giu'), ' 90 minutes '],
  [rx(String.raw`\bhalf\s+an\s+hour\b`, 'giu'), ' 30 minutes '],
  [rx(String.raw`\bhalf\s+a\s+minute\b`, 'giu'), ' 30 seconds '],
];

function unitSeconds(tok: string, afterNumber: boolean): number {
  if (/^(?:час|часа|часов|hours?|hrs?)$/.test(tok) || (afterNumber && /^(?:ч|h)$/.test(tok))) return 3600;
  if (/^(?:минута|минуту|минуты|минут|мин|minutes?|mins?|min)$/.test(tok) || (afterNumber && /^(?:м|m)$/.test(tok))) return 60;
  if (/^(?:секунда|секунду|секунды|секунд|сек|seconds?|secs?|sec)$/.test(tok) || (afterNumber && /^(?:с|s)$/.test(tok))) return 1;
  return 0;
}

// Единица без числа — одна: «на час», «на минуту», «for an hour»
const SINGLE_UNIT = /^(?:час|минуту|минутку|секунду|hour|minute|second)$/;

/** Длительность во фразе, секунды; 0 — не названа. */
export function parseDuration(text: string): number {
  let t = normalize(text);
  for (const [r, rep] of DURATION_PHRASES) t = t.replace(r, rep);
  t = t.replace(/(\d)([a-zа-я])/g, '$1 $2').replace(/([a-zа-я])(\d)/g, '$1 $2');
  const toks = t.split(/[\s,]+/).filter(Boolean);
  let total = 0;
  let num: number | null = null;
  for (const tok of toks) {
    if (/^\d+(?:[.,]\d+)?$/.test(tok)) {
      num = parseFloat(tok.replace(',', '.'));
      continue;
    }
    if (tok === 'полтора' || tok === 'полторы') {
      num = 1.5;
      continue;
    }
    if (has(NUM_WORDS, tok) || ((tok === 'a' || tok === 'an') && num === null)) {
      num = (num ?? 0) + (has(NUM_WORDS, tok) ? NUM_WORDS[tok] : 1);
      continue;
    }
    const unit = unitSeconds(tok, num !== null);
    if (unit) {
      if (num !== null) total += num * unit;
      else if (SINGLE_UNIT.test(tok) || tok === 'минутку') total += unit;
    }
    num = null;
  }
  return Math.round(total);
}

// ─── Дни: повтор будильника и одна дата ───

// Calendar Android: 1 — воскресенье … 7 — суббота
const WEEKDAY_STEMS: [RegExp, number][] = [
  [rx(String.raw`\bпонедельник\S*|\bmondays?\b`, 'giu'), 2],
  [rx(String.raw`\bвторник\S*|\btuesdays?\b`, 'giu'), 3],
  [rx(String.raw`\bсред(?:а|у|ам|ы)\b|\bwednesdays?\b`, 'giu'), 4],
  [rx(String.raw`\bчетверг\S*|\bthursdays?\b`, 'giu'), 5],
  [rx(String.raw`\bпятниц\S*|\bfridays?\b`, 'giu'), 6],
  [rx(String.raw`\bсуббот\S*|\bsaturdays?\b`, 'giu'), 7],
  [rx(String.raw`\bвоскресень\S*|\bsundays?\b`, 'giu'), 1],
];
const DAILY_RE = rx(String.raw`\bкаждый\s+день\b|\bежедневно\b|\bкаждое\s+утро\b|\bevery\s+(?:day|morning)\b|\bdaily\b`);
const WORKDAYS_RE = rx(
  String.raw`\bпо\s+будн(?:ям|им)\b|\bв\s+будни\b|\bпо\s+рабочим\b|\bв\s+рабочие\s+дни\b|\bweekdays\b|\bevery\s+weekday\b`,
);
const WEEKENDS_RE = rx(String.raw`\bпо\s+выходным\b|\bв\s+выходные\b|\bweekends?\b`);
const REPEAT_DAYS_RE = rx(
  String.raw`\bпо\s+(?:понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)\b|` +
    String.raw`\bкажд(?:ый|ую|ое)\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b|` +
    String.raw`\b(?:every|each)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|` +
    String.raw`\b(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)\b`,
);

/** Дни повтора будильника (Calendar), пусто — разовый. */
export function parseRepeatDays(text: string): number[] {
  const t = normalize(text);
  if (DAILY_RE.test(t)) return [1, 2, 3, 4, 5, 6, 7];
  if (WORKDAYS_RE.test(t)) return [2, 3, 4, 5, 6];
  if (WEEKENDS_RE.test(t)) return [1, 7];
  if (!REPEAT_DAYS_RE.test(t)) return [];
  const days = WEEKDAY_STEMS.filter(([r]) => {
    r.lastIndex = 0;
    return r.test(t);
  }).map(([, d]) => d);
  return [...new Set(days)].sort((a, b) => a - b);
}

export type DaySpec = { offset: number } | { weekday: number };

const DAY_OFFSET_RES: [RegExp, number][] = [
  [rx(String.raw`\bпослезавтра\b|\bday\s+after\s+tomorrow\b`), 2],
  [rx(String.raw`\bзавтра\b|\btomorrow\b`), 1],
  [rx(String.raw`\bсегодня\b|\btoday\b|\btonight\b`), 0],
];
const ONE_WEEKDAY_RE = rx(
  String.raw`\b(?:в|во|on)\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b`,
);

/** Разовая дата («завтра», «в пятницу»); с повтором — не дата. */
export function parseDay(text: string): DaySpec | null {
  const t = normalize(text);
  for (const [r, offset] of DAY_OFFSET_RES) if (r.test(t)) return { offset };
  const m = ONE_WEEKDAY_RE.exec(t);
  if (m) {
    const hit = WEEKDAY_STEMS.find(([r]) => {
      r.lastIndex = 0;
      return r.test(m[1]);
    });
    if (hit) return { weekday: hit[1] };
  }
  return null;
}

/**
 * Часы ставят только ближайшее hour:minute (без даты): совпадает ли оно с
 * названным днём. Ночью «завтра» — это наступающее утро.
 */
export function alarmDayFits(clock: ClockTime, day: DaySpec | null, now: Date = new Date()):
  { fits: true } | { fits: false; nearest: 'today' | 'tomorrow' } {
  const later = clock.hour * 60 + clock.minute > now.getHours() * 60 + now.getMinutes();
  const nearest = later ? 'today' : 'tomorrow';
  if (!day) return { fits: true };
  if ('offset' in day) {
    const off = later ? 0 : 1;
    if (day.offset === off || (day.offset === 1 && off === 0 && now.getHours() < 5)) return { fits: true };
    return { fits: false, nearest };
  }
  const when = new Date(now.getFullYear(), now.getMonth(), now.getDate() + (later ? 0 : 1));
  return when.getDay() + 1 === day.weekday ? { fits: true } : { fits: false, nearest };
}

// ─── Подпись будильника: «с подписью спорт», «labelled gym», в кавычках ───

const QUOTED_RE = /[«"“„]([^»"”“]{1,60})[»"”“]/u;
const LABEL_RE = rx(
  String.raw`\b(?:с\s+подписью|с\s+названием|под\s+названием|с\s+текстом|с\s+пометкой|labell?ed|called|named|with\s+(?:the\s+)?label)\s+([^,]{1,60})`,
);

function labelOf(original: string): string {
  const q = QUOTED_RE.exec(original);
  if (q) return q[1].trim();
  const m = LABEL_RE.exec(original);
  if (!m) return '';
  return m[1].replace(rx(String.raw`\s+(?:на|в|во|к|at|for|через|in)\s+\S.*$`), '').replace(/[.!?]+$/u, '').trim();
}

// ─── Команды ───

export type PhoneCommand =
  | { kind: 'alarm'; time: ClockTime | null; relative: number; days: number[]; day: DaySpec | null; label: string }
  | { kind: 'timer'; seconds: number; label: string }
  | { kind: 'alarms_list' }
  | { kind: 'alarm_off' }
  | { kind: 'open_app'; name: string; explicit: boolean; weak: boolean }
  | { kind: 'torch'; on: boolean };

const SET_VERB =
  String.raw`(?:постав(?:ь|ьте|ить)|завед(?:и|ите)|завести|установ(?:и|ите|ить)|созда(?:й|йте|ть)|сдела(?:й|йте|ть)|` +
  String.raw`добав(?:ь|ьте|ить)|включ(?:и|ите|ить)|выстав(?:и|ите|ить))`;

const TORCH_RE = rx(
  `^${MODAL}` +
    String.raw`(?:(включ(?:и|ите|ить)|зажг(?:и|ите)|зажечь|вруб(?:и|ите|ить)|выключ(?:и|ите|ить)|погас(?:и|ите|ить)|выруб(?:и|ите|ить)|отключ(?:и|ите|ить))\s+(?:мне\s+)?(?:фонарик|фонарь|вспышку)|` +
    String.raw`(?:turn|switch)\s+(on|off)\s+(?:the\s+|my\s+)?(?:flashlight|torch)|` +
    String.raw`(?:turn|switch)\s+(?:the\s+|my\s+)?(?:flashlight|torch)\s+(on|off)|(?:flashlight|torch)\s+(on|off))\b`,
);
const ALARM_OFF_RE = rx(
  `^${MODAL}` +
    String.raw`(?:(?:отмен(?:и|ите|ить)|удал(?:и|ите|ить)|выключ(?:и|ите|ить)|отключ(?:и|ите|ить)|убер(?:и|ите)|убрать|сним(?:и|ите)|снять)\s+(?:мне\s+)?(?:(?:мой|мои|все|этот|тот)\s+)?будильник\S*|` +
    String.raw`(?:cancel|delete|remove|turn\s+off|switch\s+off|disable|clear)\s+(?:all\s+)?(?:my\s+|the\s+|an?\s+)?alarms?)\b`,
);
const ALARMS_LIST_RE = rx(
  `^${MODAL}` +
    String.raw`(?:(?:покаж(?:и|ите)|показать|откр(?:ой|ойте|ыть))\s+(?:мне\s+)?(?:(?:мои|все|список)\s+)*будильник\S*|` +
    String.raw`какие\s+(?:у\s+меня\s+)?(?:есть\s+|стоят\s+)?будильники|` +
    String.raw`(?:show|open|list)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?alarms|what\s+alarms)\b`,
);
const TIMER_RE = rx(
  `^${MODAL}` +
    String.raw`(?:(?:${SET_VERB}|запуст(?:и|ите|ить))\s+(?:мне\s+|нам\s+)?(?:(?:новый|еще\s+один)\s+)?таймер|(таймер)|засек(?:и|ите)|засечь|` +
    String.raw`(?:set|start|create|make|run)\s+(?:me\s+)?(?:\S+\s+){0,4}?timer|(timer\s+for)|(\S+\s+(?:minute|second|hour)s?\s+timer))\b`,
);
const ALARM_RE = rx(
  `^${MODAL}` +
    String.raw`(?:${SET_VERB}\s+(?:мне\s+|нам\s+)?(?:(?:новый|еще\s+один|один)\s+)?будильник|(будильник)|разбуд(?:и|ите|ить)(?:\s+(?:меня|нас))?|` +
    String.raw`(?:set|create|add|make|schedule)\s+(?:me\s+)?(?:(?:an?|the|my|new|another)\s+)*alarm(?:\s+clock)?|(alarm\s+(?:for|at|on))|wake\s+(?:me|us)(?:\s+up)?)\b`,
);
const OPEN_RE = rx(
  `^${MODAL}` +
    String.raw`(откр(?:ой|ойте|ыть)|запуст(?:и|ите|ить)|включ(?:и|ите|ить)|зайди\s+в|перейди\s+в|open|launch|start|run)\s+(.+)$`,
);

// Не приложение: файлы (и имя с расширением — «открой отчёт.pdf»), сайты и
// страницы — у ядра свои команды, режим управления, фигуры речи («открой
// мне тайну», «открой окно»)
const NOT_APP_RE = rx(
  String.raw`\b(?:файл\S*|документ\S*|папк\S*|сайт\S*|страниц\S*|ссылк\S*|вкладк\S*|режим\S*|окн[оа]|глаз\S*|рот|двер\S*|` +
    String.raw`секрет\S*|тайн\S*|душ[уа]|сердц\S*|правд\S*|file|files|document|folder|website|site|page|link|tab|mode|window|door)\b|` +
    String.raw`https?:|www\.|\.(?:com|ru|org|net|io)\b|` +
    String.raw`\.(?:pdf|docx?|xlsx?|pptx?|odt|rtf|txt|md|csv|epub|djvu|zip|rar|7z|jpe?g|png|heic|mp3|mp4|mov)$`,
);
const APP_WORD_RE = rx(String.raw`^(?:(?:мне|нам|me|up|the)\s+)*(?:приложение|программу|прогу|апп|app(?:lication)?)\b\s*`);
// «the camera app»
const APP_WORD_TAIL_RE = rx(String.raw`\s+(?:app|application|приложение)$`);
const NAME_LEAD_RE = rx(String.raw`^(?:(?:мне|нам|me|up|the|my)\s+)+`);

/**
 * Команда телефону в реплике или null (обычная реплика). names — обращения
 * к персоне в начале («Коннор, поставь…»).
 */
export function parsePhoneCommand(text: string, names: string[] = []): PhoneCommand | null {
  const original = text.trim();
  if (!original || original.length > 200) return null;
  const t = prepare(original, names);
  if (!t || PC_TARGET_RE.test(t) || NEGATION_RE.test(t)) return null;

  let m = TORCH_RE.exec(t);
  if (m) {
    const off = /^(?:выкл|погас|выруб|отключ)/.test(m[1] ?? '') || [m[2], m[3], m[4]].includes('off');
    return { kind: 'torch', on: !off };
  }
  if (ALARM_OFF_RE.test(t)) return { kind: 'alarm_off' };
  if (ALARMS_LIST_RE.test(t) && !parseClock(t)) return { kind: 'alarms_list' };

  m = TIMER_RE.exec(t);
  if (m) {
    let seconds = parseDuration(t);
    if (!seconds) {
      // «таймер на 5» — минуты
      const bareMin = rx(String.raw`(?:на|for)\s+(\d{1,3})\s*$`).exec(t);
      if (bareMin) seconds = parseInt(bareMin[1], 10) * 60;
    }
    // «таймер …» без глагола — только с длительностью: «таймер сломался» — разговор
    if (!seconds && (m[1] || m[2] || m[3])) return null;
    return { kind: 'timer', seconds: Math.min(seconds, 86400), label: labelOf(original) };
  }

  m = ALARM_RE.exec(t);
  if (m) {
    const days = parseRepeatDays(t);
    const label = labelOf(original);
    const day = days.length ? null : parseDay(t);
    // «через 20 минут», «на 20 минут», «in 2 hours» — будильник через столько
    const rel = rx(String.raw`(?:^|\s)(?:через|in)\s+(.+)$`).exec(t);
    let relative = rel ? parseDuration(rel[1]) : 0;
    if (!relative) {
      const onMin = rx(String.raw`(?:^|\s)на\s+((?:\d+|${NUM_PHRASE}|полчаса|полторы)\s*(?:минут\S*|мин|секунд\S*|сек|полчаса)?)(?:\s|$)`).exec(t);
      if (onMin && /мин|сек|полчаса/.test(onMin[1])) relative = parseDuration(onMin[1]);
    }
    const time = relative ? null : parseClock(t);
    // «будильник …» без глагола — только со временем: «будильник не звонит» — разговор
    if (!time && !relative && (m[1] || m[2])) return null;
    return { kind: 'alarm', time, relative, days, day, label };
  }

  m = OPEN_RE.exec(t);
  if (m) {
    const verb = m[1];
    let name = m[2].replace(PHONE_WORDS_RE, ' ').replace(/\s+/g, ' ').trim();
    const appWord = APP_WORD_RE.test(name) || APP_WORD_TAIL_RE.test(name);
    name = name.replace(APP_WORD_RE, '').replace(NAME_LEAD_RE, '').replace(APP_WORD_TAIL_RE, '');
    name = name.replace(/^[«"“„']+|[»"”“'.!?,]+$/gu, '').trim();
    if (!name || NOT_APP_RE.test(name) || name.split(/\s+/).length > 4) return null;
    const known = aliasGroup(name) !== null;
    const latin = /^[a-z0-9 .+&'-]+$/.test(name);
    // «включи», «start»: «включи музыку» — скорее просьба играть, чем
    // открыть «Музыку»; приложение — только по точному названию бренда
    const weak = /^(?:включ|start|run)/.test(verb) && !appWord;
    const explicit = appWord || /^(?:запуст|launch)/.test(verb) || known || latin;
    return { kind: 'open_app', name, explicit, weak };
  }
  return null;
}

// ─── Ответ на переспрос ───

/** Время будильника в ответе на «на сколько поставить?». */
export function parseAlarmAnswer(text: string): { time: ClockTime | null; relative: number } | null {
  const t = normalize(text);
  const rel = rx(String.raw`^(?:через|in)\s+(.+)$`).exec(t);
  if (rel) {
    const relative = parseDuration(rel[1]);
    if (relative) return { time: null, relative };
  }
  const time = parseClock(t, true);
  return time ? { time, relative: 0 } : null;
}

/** Длительность таймера в ответе на «на сколько?»; голое число — минуты. */
export function parseTimerAnswer(text: string): number {
  const t = normalize(text).replace(/^(?:на|for)\s+/, '');
  const d = parseDuration(t);
  if (d) return Math.min(d, 86400);
  const n = numberOf(t);
  return n !== null && n > 0 && n <= 1440 ? n * 60 : 0;
}

const CHOICE_WORDS: Record<string, number> = {
  первый: 1, первое: 1, первую: 1, первая: 1, first: 1,
  второй: 2, второе: 2, вторую: 2, вторая: 2, second: 2,
  третий: 3, третье: 3, третью: 3, третья: 3, third: 3,
  четвертый: 4, четвертое: 4, четвертую: 4, четвертая: 4, fourth: 4,
  пятый: 5, пятое: 5, пятую: 5, пятая: 5, fifth: 5,
  шестой: 6, шестое: 6, шестую: 6, шестая: 6, sixth: 6,
};

/** Номер варианта в ответе («2», «второе», «the second one»), с 0; нет — null. */
export function parseChoice(text: string, count: number): number | null {
  const t = normalize(text).replace(/[.!)]+$/u, '').replace(/^(?:№|номер|number|the)\s*/, '').replace(/\s+(?:one|вариант)$/, '').trim();
  let n: number | null = null;
  if (/^\d{1,2}$/.test(t)) n = parseInt(t, 10);
  else if (has(CHOICE_WORDS, t)) n = CHOICE_WORDS[t];
  else if (/^(?:1st|2nd|3rd|[4-6]th)$/.test(t)) n = parseInt(t, 10);
  return n !== null && n >= 1 && n <= count ? n - 1 : null;
}

const DECLINE_RE = rx(
  String.raw`^(?:не\s+надо|не\s+нужно|нет|отмена|отмени|забудь|неважно|не\s+важно|передумал[аи]?|no|nope|cancel|never\s*mind|forget\s+it)(?:[\s,]+(?:спасибо|thanks|thank\s+you))?[\s.!…]*$`,
);

/** «не надо» / «отмена» в ответ на переспрос. */
export function isDecline(text: string): boolean {
  return DECLINE_RE.test(normalize(text));
}

// ─── Подбор приложения по названию ───

export interface AppEntry {
  label: string;
  package: string;
}

// Одно и то же приложение разными словами: «ютуб» — YouTube, «камеру» —
// «Камера» на русском телефоне и Camera на английском
const ALIAS_GROUPS: string[][] = [
  ['ютуб', 'ютюб', 'youtube'],
  ['ютуб музыка', 'youtube music', 'yt music'],
  ['вотсап', 'ватсап', 'вацап', 'воцап', 'whatsapp'],
  ['телега', 'телеграм', 'телеграмм', 'telegram'],
  ['вк', 'вконтакте', 'vk', 'vkontakte'],
  ['инста', 'инстаграм', 'instagram'],
  ['хром', 'гугл хром', 'chrome', 'google chrome'],
  ['гугл', 'google'],
  ['плей маркет', 'плеймаркет', 'гугл плей', 'плей стор', 'google play', 'play store', 'play маркет'],
  ['вайбер', 'viber'],
  ['зум', 'zoom'],
  ['скайп', 'skype'],
  ['фейсбук', 'facebook'],
  ['твиттер', 'twitter'],
  ['тикток', 'тик ток', 'tiktok'],
  ['спотифай', 'spotify'],
  ['нетфликс', 'netflix'],
  ['дискорд', 'discord'],
  ['яндекс', 'yandex'],
  ['почта', 'gmail', 'mail', 'email', 'почта mail', 'электронная почта'],
  ['камера', 'camera'],
  ['галерея', 'gallery', 'фото', 'photos', 'google фото', 'фотографии'],
  ['настройки', 'settings'],
  ['калькулятор', 'calculator'],
  ['календарь', 'calendar'],
  ['часы', 'clock'],
  ['заметки', 'notes', 'keep', 'google keep'],
  ['контакты', 'contacts'],
  ['сообщения', 'смс', 'messages', 'sms'],
  ['телефон', 'звонилка', 'phone', 'dialer'],
  ['браузер', 'browser', 'chrome', 'firefox', 'яндекс браузер', 'opera'],
  ['карты', 'maps', 'google maps', 'яндекс карты'],
  ['музыка', 'music'],
  ['погода', 'weather'],
  ['диктофон', 'recorder', 'voice recorder', 'запись звука'],
];

const TRANSLIT: Record<string, string> = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm',
  н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch', ш: 'sh',
  щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya',
};

const translit = (s: string) => [...s].map((c) => TRANSLIT[c] ?? c).join('');
const clean = (s: string) => normalize(s).replace(/[^\p{L}\p{N} ]+/gu, ' ').replace(/\s+/g, ' ').trim();
const compact = (s: string) => s.replace(/\s+/g, '');

/** Винительный → именительный по словам: «камеру» → «камера», «галерею» → «галерея». */
function nominative(s: string): string {
  return s
    .split(' ')
    .map((w) => (w.length >= 3 && /[а-я]у$/.test(w) ? `${w.slice(0, -1)}а` : w.length >= 3 && /[а-я]ю$/.test(w) ? `${w.slice(0, -1)}я` : w))
    .join(' ');
}

function aliasGroup(name: string): string[] | null {
  const forms = new Set([clean(name), nominative(clean(name))]);
  const out = new Set<string>();
  for (const g of ALIAS_GROUPS) if (g.some((a) => forms.has(a))) for (const a of g) out.add(a);
  return out.size ? [...out] : null;
}

// Бренды, которые «включи X» открывает: в группе есть латинское имя, а не
// только общее слово («музыка», «погода»)
const GENERIC = new Set(['камера', 'галерея', 'настройки', 'калькулятор', 'календарь', 'часы', 'заметки', 'контакты',
  'сообщения', 'телефон', 'браузер', 'карты', 'музыка', 'погода', 'диктофон', 'почта']);

function levenshtein(a: string, b: string): number {
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const tmp = prev[j];
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1));
      diag = tmp;
    }
  }
  return prev[b.length];
}

function similarity(a: string, b: string): number {
  if (!a || !b) return 0;
  return 1 - levenshtein(a, b) / Math.max(a.length, b.length);
}

/** Насколько название приложения подходит к названному: 0…1. Варианты со
 * своим весом: слова человека — 1, синонимы из ALIAS_GROUPS — чуть меньше
 * («открой chrome» — это Chrome, а не любой браузер из группы). */
function score(variants: [string, number][], label: string): number {
  const l = clean(label);
  const forms = [...new Set([l, translit(l)])];
  let best = 0;
  for (const [v, w] of variants) {
    for (const f of forms) {
      let s = 0;
      if (v === f) s = 1;
      else if (compact(v) === compact(f)) s = 0.98;
      // «яндекс» — «Яндекс Музыка»: название начинается названным словом
      else if (f.startsWith(`${v} `)) s = 0.8;
      else if (f.split(' ').includes(v)) s = 0.75;
      else if (v.length >= 4 && compact(f).startsWith(compact(v))) s = 0.7;
      const sim = similarity(compact(v), compact(f));
      if (sim >= 0.8) s = Math.max(s, sim * 0.95);
      best = Math.max(best, s * w);
    }
  }
  return best;
}

export type AppMatch =
  | { status: 'ok'; app: AppEntry }
  | { status: 'ambiguous'; apps: AppEntry[] }
  | { status: 'not_found' };

/** Приложение по названию из реплики. weak — «включи X»: только бренд по
 * точному названию («включи ютуб»), «включи музыку» — не приложение. */
export function matchApp(name: string, apps: AppEntry[], weak = false): AppMatch {
  const base = clean(name);
  const group = aliasGroup(name);
  if (weak && (!group || group.some((a) => GENERIC.has(a)))) return { status: 'not_found' };
  const own = [...new Set([base, nominative(base), translit(base), translit(nominative(base))])].filter(Boolean);
  const variants: [string, number][] = [
    ...own.map((v): [string, number] => [v, 1]),
    ...(group ?? []).filter((a) => !own.includes(a)).map((a): [string, number] => [a, 0.97]),
  ];
  const scored = apps
    .map((app) => ({ app, s: score(variants, app.label) }))
    .filter((x) => x.s >= (weak ? 0.9 : 0.66))
    .sort((a, b) => b.s - a.s);
  if (!scored.length) return { status: 'not_found' };
  const top = scored[0].s;
  // Ровно названное (своими словами или синонимом) — без вопросов, если одно
  const exact = scored.filter((x) => x.s >= top - 0.001 && x.s >= 0.9);
  if (exact.length === 1) return { status: 'ok', app: exact[0].app };
  if (exact.length > 1) return { status: 'ambiguous', apps: exact.slice(0, 6).map((x) => x.app) };
  if (scored.length === 1 || top - scored[1].s >= 0.1) return { status: 'ok', app: scored[0].app };
  return { status: 'ambiguous', apps: scored.filter((x) => top - x.s < 0.1).slice(0, 6).map((x) => x.app) };
}

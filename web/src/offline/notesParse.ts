/* Заметки без ядра: разбор просьбы («запиши в заметки …», «удали заметку 2»,
   «что у меня в заметках?») и тексты для модели — перенос
   app/features/user_notes.py (parse_request, resolve_delete, render_list,
   notes_for_prompt, prompt_*). Запись и удаление делает код, модели — только
   итог, как у ядра. Без зависимостей приложения: scripts/test_offline_notes.py
   сверяет модуль с ядром под Node.

   Регулярки Python → JS: \w и [^\W\d_] ядра — буквы Unicode (флаг u), \b —
   граница по буквам Unicode (B ниже: в JS \b знает только латиницу). */

export interface NotesRequest {
  // add — записать; delete — удалить; show — показать список; ask — реплика
  // про заметки (вопрос по ним) — модель отвечает по списку
  kind: 'add' | 'delete' | 'show' | 'ask';
  text: string; // add: текст заметки; delete: что удалить
  ref: boolean; // add: «запиши это» — текст из прошлого сообщения
  all: boolean; // delete: «все заметки»
}

export interface NoteLike {
  id: string;
  text: string;
  created_at: number; // unix-секунды
}

export const MAX_NOTES = 500;
export const MAX_TEXT = 4000;
const CONTEXT_NOTE_MAX = 1200;
const CONTEXT_TOTAL_MAX = 6000;
const LIST_NOTE_MAX = 400;
const LIST_TOTAL_MAX = 3500;

// Буква/цифра/«_» по Unicode — \w Python
const W = String.raw`[\p{L}\p{N}\p{M}_]`;
// \b Python (Unicode)
const B = String.raw`(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;
// (?<![^\W\d_]) / (?![^\W\d_]) — не после / не перед буквой
const NL = String.raw`(?<!\p{L})`;
const NR = String.raw`(?!\p{L})`;

const rx = (src: string, flags: string) => new RegExp(src, `${flags}u`);
const I = 'i';
const IS = 'is';

const RU_NOUN = String.raw`(?:заметк${W}*|заметок|блокнот${W}*)`;
const EN_NOUN = String.raw`(?:notes|notebook|notepad)`;
const ANY_NOUN_RE = rx(String.raw`${NL}${RU_NOUN}|${B}(?:notes?|notebook|notepad)${B}`, I);

const RU_ADD_VERB =
  String.raw`(?:запиши|запишите|записывай|занеси|занесите|внеси|внесите|впиши|впишите|` +
  String.raw`добавь|добавьте|сохрани|сохраните|запомни|запомните|черкни|закинь|` +
  String.raw`(?:можешь|можете|сможешь|мог(?:ла|ли)?\s+бы)\s+(?:ты\s+|вы\s+)?(?:мне\s+)?` +
  String.raw`(?:записать|занести|внести|вписать|добавить|сохранить))(?:-ка)?`;
const RU_TARGET =
  String.raw`(?:в|во)\s+(?:мои\s+|свои\s+|наши\s+|твои\s+|мой\s+|свой\s+|твой\s+)?` + RU_NOUN;
const RU_ADD_RE = rx(
  String.raw`^(?<lead>.*?)${NL}(?<verb>${RU_ADD_VERB})${NR}(?<mid>.*?)` +
    String.raw`${NL}(?<target>${RU_TARGET})${NR}(?<post>.*)$`,
  IS,
);
const RU_ADD_REV_RE = rx(
  String.raw`^(?<lead>.*?)${NL}(?<target>${RU_TARGET})\s+(?:мне\s+|себе\s+)?` +
    String.raw`(?<verb>${RU_ADD_VERB})${NR}(?<post>.*)$`,
  IS,
);
const RU_NOTE_OBJ_RE = rx(
  String.raw`^(?<lead>.*?)${NL}(?:(?:сделай|создай|оставь|добавь|запиши|напиши|заведи)\s+` +
    String.raw`(?:мне\s+|себе\s+)?(?:новую\s+|ещё\s+одну\s+|еще\s+одну\s+)?заметку|` +
    String.raw`нов(?:ая|ую)\s+заметк[ау])${NR}(?<post>.*)$`,
  IS,
);
const RU_NOTE_COLON_RE = rx(String.raw`^(?<lead>)заметка\s*[:—–-]\s*(?<post>.+)$`, IS);

const EN_ADD_VERB = String.raw`(?:add|put|write(?:\s+down)?|jot(?:\s+down)?|save|note(?:\s+down)?|record|store|keep)`;
const EN_TARGET = String.raw`(?:to|in|into|on)\s+(?:my\s+|the\s+|your\s+|our\s+)?${EN_NOUN}`;
const EN_ADD_RE = rx(
  String.raw`^(?<lead>.*?)${B}(?<verb>${EN_ADD_VERB})${B}(?<mid>.*?)${B}(?<target>${EN_TARGET})${B}(?<post>.*)$`,
  IS,
);
const EN_NOTE_OBJ_RE = rx(
  String.raw`^(?<lead>.*?)${B}(?:(?:take|make|add|create|leave|write|jot\s+down)\s+` +
    String.raw`(?:me\s+)?(?:a|another)\s+(?:new\s+|quick\s+)?note|new\s+note)${B}` +
    String.raw`(?:\s+(?:of|that|about)${B})?(?<post>.*)$`,
  IS,
);

// Отрицание перед глаголом: «не записывай это в заметки», «don't add …»
const NEG_TAIL_RE = rx(
  String.raw`(?:${NL}не\s+(?:надо\s+|нужно\s+|стоит\s+|будем\s+)?|` +
    String.raw`${B}(?:don['’]?t|do\s+not|never|no\s+need\s+to)\s+(?:${W}+\s+)?)$`,
  I,
);

const FILLER =
  String.raw`(?:мне|нам|себе|у\s+себя|пожалуйста|пжлст|пж|плиз|плз|please|pls|for\s+me|` +
  String.raw`быстренько|быстро|ка|down|it\s+down)`;
const LEAD_FILLER_RE = rx(String.raw`^(?:[\s,:;.\-—–!]+|${FILLER}${NR})+`, I);
const TAIL_WORD_RE = rx(String.raw`${NL}(?:${FILLER}|спасибо|thanks)$`, I);
const TAIL_SEP = ' \t\r\n,;-—–';
const SCAN = 400;
const POLITE_LEAD_RE = rx(
  String.raw`^(?:\s*(?:please|pls|can|could|would|will|you|hey|ok|okay|and|also|now|just|so|` +
    String.raw`слушай|а|и|ну|так|ещё|еще|пожалуйста|давай|ты|вы|можешь|можете|сможешь|мог|могла|` +
    String.raw`могли|бы)${NR}\s*,?)+`,
  I,
);
const LEAD_THAT_RE = rx(String.raw`^(?:(?:о\s+)?то[мй]?\s*,?\s*)?(?:что|чтобы|that)${NR}\s*,?\s*`, I);

const REF_RE = rx(
  String.raw`^(?:вот\s+)?(?:это|эту|этот|эти|этого|всё\s+это|все\s+это|это\s+всё|это\s+все|всё|все|` +
    String.raw`сказанное|написанное|последнее|` +
    String.raw`this|that|it|these|those|everything|all\s+of\s+(?:this|that|it))` +
    String.raw`(?:\s+\p{L}[${W.slice(1, -1)}-]*){0,2}\s*[.!?…]*$`,
  I,
);
const REF_YOU_RE = rx(
  String.raw`^(?:(?:всё|все|то|это)\s*,?\s*(?:что\s+)?)?(?:(?:ты|вы)\s+(?:только\s+что\s+|сейчас\s+|выше\s+|мне\s+)*` +
    String.raw`(?:сказал|написал|дал|прислал|предложил|перечислил|посоветовал|скинул|ответил|рассказал)${W}*` +
    String.raw`|(?:твой|твоё|твое|твоего|последний)\s+(?:последний\s+)?(?:ответ|сообщени${W}*)` +
    String.raw`|(?:all\s+of\s+)?what\s+you\s+(?:just\s+)?(?:said|wrote|sent|told|suggested|listed)` +
    String.raw`|your\s+(?:last\s+)?(?:answer|message|reply)` +
    String.raw`|выше|above)(?:\s+[^\s.!?…]+){0,3}\s*[.!?…]*$`,
  I,
);

const RU_DEL_RE = rx(
  String.raw`${NL}(?:удали|удалите|убери|уберите|вычеркни|вычеркните|сотри|сотрите|` +
    String.raw`выкинь|выбрось|почисти|очисти|очистите)(?:-ка)?${NR}(?<rest>.*)$`,
  IS,
);
const EN_DEL_RE = rx(String.raw`${B}(?:delete|remove|erase|clear|cross\s+out|drop|wipe)${B}(?<rest>.*)$`, IS);
const DEL_NOISE_RE = rx(
  String.raw`${NL}(?:из|в|у|с)\s+(?:моих\s+|своих\s+|мои\s+)?${RU_NOUN}` +
    String.raw`|${NL}(?:мои|свои|моих|все|всё|эту|эти|одну|мне|пожалуйста|номер|под\s+номером)${NR}` +
    String.raw`|${RU_NOUN}` +
    String.raw`|${NL}(?:про|о|об|где|которая|которую|в\s+которой|насчёт|насчет|с\s+текстом|со\s+словами)${NR}` +
    String.raw`|${B}(?:from|in)\s+(?:my\s+|the\s+)?${EN_NOUN}${B}` +
    String.raw`|${B}(?:my|the|all|a|note|notes|number|please|about|that\s+says|with|saying)${B}` +
    String.raw`|[№#]`,
  'gi',
);
const DEL_ALL_RE = rx(String.raw`${NL}(?:все|всё)${NR}|${B}all${B}`, I);
const CLEAR_VERB_RE = rx(String.raw`${NL}(?:почисти|очисти|очистите)${NR}|${B}(?:clear|wipe)${B}`, I);

const SHOW_END_RE = rx(String.raw`(?:${RU_NOUN}|${EN_NOUN})\s*[?.!…]*\s*$`, I);
const SHOW_WORD_RE = rx(
  String.raw`${NL}(?:покажи|покажите|прочитай|прочти|прочитайте|зачитай|выведи|перечисли|` +
    String.raw`дай|скинь|пришли|напомни|глянь|посмотри|проверь|что|чего|какие|список|мои|все)${NR}` +
    String.raw`|${B}(?:show|read|list|give|send|tell|check|see|what|whats|what's|my)${B}` +
    String.raw`|^\s*(?:заметки|notes)\s*[?.!…]*\s*$`,
  I,
);
const APP_CMD_RE = rx(String.raw`^\s*(?:открой|запусти|закрой|open|launch|close|start)${B}`, I);
const EN_MENTION_RE = rx(
  String.raw`${B}(?:my|the|your|our)\s+${EN_NOUN}${B}|${B}in\s+(?:my\s+)?notes${B}|${B}(?:notebook|notepad)${B}`,
  I,
);
const RU_MENTION_RE = rx(String.raw`${NL}${RU_NOUN}`, I);
const POLITE_Q_RE = rx(
  String.raw`${NL}(?:можешь|можете|сможешь|мог(?:ла|ли)?\s+бы)${NR}|${B}(?:can|could|would)\s+you${B}`,
  I,
);

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\/-]/g, '\\$&');

function namesAlt(names: Iterable<string>): string {
  const ws = [...new Set([...names].map((n) => String(n ?? '').trim()).filter((n) => n.length >= 2))];
  ws.sort((a, b) => b.length - a.length);
  return ws.map(escapeRe).join('|');
}

/** «Коннор, запиши …», «эй Коннор …» → «запиши …» (strip_address ядра). */
export function stripAddress(text: string, names: Iterable<string> = []): string {
  const alt = namesAlt(names);
  const lead = String.raw`\s*(?:(?:hey|hi|ok|okay|эй|слушай|так)${B}\s*,?\s*)?`;
  if (alt) text = text.replace(rx(String.raw`^(?:${lead}(?:${alt})(?![${W.slice(1, -1)}'’-])[\s,:;!.…—–-]*)+`, I), '');
  return text.trim();
}

function rstripChars(s: string, chars: string): string {
  let end = s.length;
  while (end > 0 && chars.includes(s[end - 1])) end--;
  return s.slice(0, end);
}

function stripTail(part: string): string {
  // «… пожалуйста», «… мне, спасибо» — по слову с конца
  for (;;) {
    let cut = rstripChars(part, TAIL_SEP);
    const win = cut.slice(-40);
    const m = TAIL_WORD_RE.exec(win);
    if (m) cut = cut.slice(0, cut.length - (win.length - m.index));
    if (cut === part) return part;
    part = cut;
  }
}

function clean(part: string): string {
  part = part || '';
  const head = part.slice(0, 200);
  const body = part.slice(200);
  return stripTail(head.replace(LEAD_FILLER_RE, '') + body).trim();
}

function leadSep(leadRaw: string): boolean {
  // Текст перед глаголом отделён знаком: «Мука, яйца — запиши в заметки»
  const tail = rstripChars(leadRaw, ' \t');
  return tail.endsWith('\n') || ['—', '–', ':', '-', '.', '!'].includes(tail.trimEnd().slice(-1));
}

function isRef(part: string): boolean {
  return !!part && part.length <= 60 && (REF_RE.test(part) || REF_YOU_RE.test(part));
}

const cap = (text: string) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

type Groups = Record<string, string>;

function req(kind: NotesRequest['kind'], over: Partial<NotesRequest> = {}): NotesRequest {
  return { kind, text: '', ref: false, all: false, ...over };
}

function addRequest(gd: Groups, politeQ: boolean): NotesRequest {
  const leadRaw = gd.lead || '';
  const lead = clean(leadRaw.replace(POLITE_LEAD_RE, ''));
  let mid = clean(gd.mid || '');
  const postRaw = gd.post || '';
  let post = clean(postRaw.replace(LEAD_FILLER_RE, '').replace(LEAD_THAT_RE, ''));
  if (politeQ) {
    post = rstripChars(post, '?').trimEnd();
    mid = rstripChars(mid, '?').trimEnd();
  }
  mid = clean(mid.replace(LEAD_THAT_RE, ''));
  let text: string;
  let ref = false;
  if (post && mid && !isRef(mid) && !isRef(post)) {
    const sep = /^\s*[:—–-]/.test(postRaw) ? ': ' : ' ';
    text = `${mid}${sep}${post}`;
  } else {
    text = post && !isRef(post) ? post : mid && !isRef(mid) ? mid : '';
    ref = !text && (isRef(post) || isRef(mid));
  }
  if (!text && lead && leadSep(leadRaw)) {
    text = lead;
    ref = false;
  }
  return req('add', { text: cap(text), ref });
}

const PUNCT_EDGE = ' ,.:;!?…-—–';

function deleteRequest(rest: string, clearVerb: boolean): NotesRequest {
  let target = rest.replace(DEL_NOISE_RE, ' ');
  target = target.replace(/\s+/gu, ' ');
  let a = 0;
  let b = target.length;
  while (a < b && PUNCT_EDGE.includes(target[a])) a++;
  while (b > a && PUNCT_EDGE.includes(target[b - 1])) b--;
  target = target.slice(a, b);
  const all = DEL_ALL_RE.test(rest) || (clearVerb && !target);
  return req('delete', { text: target, all });
}

function groupsOf(m: RegExpExecArray): Groups {
  const out: Groups = {};
  for (const [k, v] of Object.entries(m.groups ?? {})) out[k] = v ?? '';
  return out;
}

function addGroups(re: RegExp, t: string): Groups | null {
  // Длинный текст — просьба в первых или последних SCAN символах
  if (t.length <= 2 * SCAN) {
    const m = re.exec(t);
    return m ? groupsOf(m) : null;
  }
  let m = re.exec(t.slice(0, SCAN));
  if (m) {
    const gd = groupsOf(m);
    gd.post = (gd.post || '') + t.slice(SCAN);
    return gd;
  }
  const start = t.length - SCAN;
  m = re.exec(t.slice(start));
  if (m) {
    const gd = groupsOf(m);
    gd.lead = t.slice(0, start) + (gd.lead || '');
    return gd;
  }
  return null;
}

/** Просьба про заметки в реплике или null. names — обращения к персоне. */
export function parseNotesRequest(text: string, names: Iterable<string> = []): NotesRequest | null {
  if (!text || !ANY_NOUN_RE.test(text)) return null;
  const t = stripAddress(String(text), names);
  if (!t) return null;
  const head = t.split('\n', 1)[0].slice(0, SCAN);
  const politeQ = POLITE_Q_RE.test(head);

  // Удаление — раньше записи: «удали из заметок …» не текст новой заметки
  for (const re of [RU_DEL_RE, EN_DEL_RE]) {
    const m = re.exec(head);
    if (m && ANY_NOUN_RE.test(m.groups?.rest ?? '') && !NEG_TAIL_RE.test(head.slice(0, m.index))) {
      const rest = (m.groups?.rest ?? '') + t.slice(head.length);
      return deleteRequest(rest, CLEAR_VERB_RE.test(m[0]));
    }
  }

  for (const re of [RU_ADD_RE, RU_ADD_REV_RE, RU_NOTE_OBJ_RE, RU_NOTE_COLON_RE, EN_ADD_RE, EN_NOTE_OBJ_RE]) {
    const gd = addGroups(re, t);
    if (gd === null) continue;
    const leadRaw = gd.lead || '';
    if (NEG_TAIL_RE.test(leadRaw.slice(-40))) return req('ask');
    // Английский глагол — повелительный, в начале фразы
    if ((re === EN_ADD_RE || re === EN_NOTE_OBJ_RE) && leadRaw.slice(0, SCAN).replace(POLITE_LEAD_RE, '').trim() && !leadSep(leadRaw)) {
      continue;
    }
    if (leadRaw.trim().includes('\n') && (gd.post || '').trim().includes('\n')) break;
    return addRequest(gd, politeQ);
  }

  if (APP_CMD_RE.test(t)) return null;
  if (t.length <= 80 && SHOW_END_RE.test(t) && SHOW_WORD_RE.test(t)) return req('show');
  if (RU_MENTION_RE.test(t) || EN_MENTION_RE.test(t)) return req('ask');
  return null;
}

// ── Удаление: номер или текст ─────────────────────────────────────────

const ENDING_RE =
  /(?:ами|ями|ого|его|ому|ему|ыми|ими|ую|юю|ая|яя|ой|ей|ое|ее|ые|ие|ый|ий|ым|им|ом|ем|ах|ях|у|ю|а|я|о|е|ы|и|й|ь|s)$/u;

function stems(text: string): string[] {
  const words = (text || '').toLowerCase().replaceAll('ё', 'е').match(/[\p{L}\p{N}\p{M}]+/gu) ?? [];
  return words.map((w) => (w.length > 4 ? w.replace(ENDING_RE, '') : w));
}

const NUMS_RE = /^\s*\d+(?:\s*[-–—]\s*\d+)?(?:\s*(?:,|и|and|;)\s*\d+(?:\s*[-–—]\s*\d+)?)*\s*$/iu;
const LAST_RE = rx(String.raw`^(?:последн${W}*|last|latest|the\s+last\s+one)$`, I);
const FIRST_RE = rx(String.raw`^(?:перв${W}*|first)$`, I);

/** Какие заметки удалить (notes — по порядку записи; номера — его строки).
 * null — непонятно какие. */
export function resolveDelete<T extends NoteLike>(r: NotesRequest, notes: T[]): T[] | null {
  const target = (r.text || '').trim();
  if (!notes.length) return [];
  if (!target) return r.all ? [...notes] : null;
  if (LAST_RE.test(target)) return [notes[notes.length - 1]];
  if (FIRST_RE.test(target)) return [notes[0]];
  if (NUMS_RE.test(target)) {
    const idx = new Set<number>();
    for (const m of target.matchAll(/(\d+)(?:\s*[-–—]\s*(\d+))?/gu)) {
      let lo = Number(m[1]);
      let hi = Number(m[2] || m[1]);
      [lo, hi] = [Math.min(lo, hi), Math.max(lo, hi)];
      if (hi - lo < 50) for (let i = lo; i <= hi; i++) idx.add(i);
    }
    const picked = [...idx].sort((a, b) => a - b).filter((i) => i >= 1 && i <= notes.length).map((i) => notes[i - 1]);
    return picked.length ? picked : null;
  }
  const want = stems(target).filter((s) => s.length >= 2);
  if (!want.length) return null;
  const matched = notes.filter((n) => {
    const words = stems(n.text || '');
    return want.every((s) => words.some((w) => w.startsWith(s)));
  });
  if (r.all) return matched.length ? matched : null;
  return matched.length === 1 ? matched : null;
}

// ── Тексты для пользователя и для модели ──────────────────────────────

function clip(text: string, limit: number): string {
  const s = String(text ?? '').trim();
  return s.length <= limit ? s : `${s.slice(0, limit - 1).trimEnd()}…`;
}

const pad = (n: number) => String(n).padStart(2, '0');

/** «12.10» — дата заметки по часам телефона (у ядра — по поясу пользователя). */
export function noteDate(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return '';
  const d = new Date(ts * 1000);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
}

const LIST_TITLE: Record<string, string> = { ru: '📝 Заметки:', en: '📝 Notes:' };
const LIST_MORE: Record<string, string> = {
  ru: '…и ещё {n} — все заметки на главной странице.',
  en: '…and {n} more — all notes are on the home page.',
};
const LIST_EMPTY: Record<string, string> = { ru: 'Заметок пока нет.', en: 'No notes yet.' };

/** Список сообщением: «1. текст · 12.10»; длинные — обрезаны. */
export function renderNotesList(notes: NoteLike[], lang?: string | null): string {
  const l = lang && lang in LIST_TITLE ? lang : 'ru';
  if (!notes.length) return LIST_EMPTY[l];
  const lines = [LIST_TITLE[l]];
  let total = 0;
  for (let i = 1; i <= notes.length; i++) {
    const n = notes[i - 1];
    const body = clip(n.text, LIST_NOTE_MAX).replaceAll('\n', '\n   ');
    const d = noteDate(n.created_at);
    const line = `${i}. ${body}${d ? ` · ${d}` : ''}`;
    if (total + line.length > LIST_TOTAL_MAX && i > 1) {
      lines.push(LIST_MORE[l].replace('{n}', String(notes.length - i + 1)));
      break;
    }
    lines.push(line);
    total += line.length;
  }
  return lines.join('\n');
}

/** Нумерованные заметки для промпта: при нехватке места выпадают старые. */
export function notesForPrompt(notes: NoteLike[]): string {
  const lines: string[] = [];
  let total = 0;
  for (let i = notes.length; i >= 1; i--) {
    const n = notes[i - 1];
    const d = noteDate(n.created_at);
    const line = d ? `${i}. [${d}] ${clip(n.text, CONTEXT_NOTE_MAX)}` : `${i}. ${clip(n.text, CONTEXT_NOTE_MAX)}`;
    if (total + line.length > CONTEXT_TOTAL_MAX && lines.length) {
      lines.push(`(+${i} older notes not shown)`);
      break;
    }
    lines.push(line);
    total += line.length;
  }
  return lines.reverse().join('\n');
}

export const NOTES_RULE =
  "Notes are the user's own notebook kept by the system for you. Never " +
  'invent notes, never claim you cannot keep notes, and do not mention ' +
  'files, the system or technical details.';

export function promptSaved(text: string): string {
  return (
    "You have just written this into the user's notes — it is " +
    `already saved:\n«${clip(text, 300)}»\n` +
    'Confirm briefly, in your own style, that it is written down. Do ' +
    'not repeat the whole note back. ' +
    NOTES_RULE
  );
}

export function promptNothingToSave(): string {
  return (
    'The user wants to save something to their notes but did not say ' +
    'what. Nothing was saved. Ask briefly what to write down. ' +
    NOTES_RULE
  );
}

export function promptFull(): string {
  return (
    `The user's notes are full (${MAX_NOTES} notes), so nothing was ` +
    'saved. Say so briefly: old notes can be deleted on the home page ' +
    'or with «удали заметку N». ' +
    NOTES_RULE
  );
}

export function promptDeleted(removed: NoteLike[], left: number): string {
  const items = removed.map((n) => `- ${clip(n.text, 200)}`).join('\n');
  return `You have just deleted these from the user's notes:\n${items}\n${left} notes are left. Confirm briefly, in your own style. ` + NOTES_RULE;
}

export function promptDeleteUnclear(notes: NoteLike[]): string {
  if (!notes.length) {
    return "The user asked to delete a note, but their notes are empty — nothing was deleted. Say so briefly. " + NOTES_RULE;
  }
  return (
    'The user asked to delete a note, but it is unclear which one — ' +
    'NOTHING was deleted. Ask which note number to delete. Their notes ' +
    `(number. [date] text):\n${notesForPrompt(notes)}\n` +
    NOTES_RULE
  );
}

export function promptShow(notes: NoteLike[]): string {
  if (!notes.length) {
    return (
      'The user asked what is in their notes, but there are no notes ' +
      'yet. Say so briefly; they can ask you «запиши в заметки …». ' +
      NOTES_RULE
    );
  }
  return (
    'The user asked to see their notes. The full list is sent to them ' +
    'automatically as a separate message right after your reply — DO ' +
    'NOT print, list or retell the notes yourself. Reply with one short ' +
    'line in your own style. For reference, their notes:\n' +
    `${notesForPrompt(notes)}\n` +
    NOTES_RULE
  );
}

export function promptAsk(notes: NoteLike[]): string {
  if (!notes.length) {
    return (
      "The user's notes are empty — there is nothing written down " +
      'yet. If they ask about their notes, say so honestly. ' +
      NOTES_RULE
    );
  }
  return (
    "The user's notes (number. [date] text), written down by them " +
    `earlier:\n${notesForPrompt(notes)}\n` +
    'If the user asks about their notes or about something they wrote ' +
    'down, answer strictly from these notes; if it is not there, say so ' +
    'honestly. If the message is not about the notes, ignore this block. ' +
    NOTES_RULE
  );
}

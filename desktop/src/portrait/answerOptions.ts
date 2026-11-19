/* Кнопки ответа у портрета: вопрос, на который персона ждёт ответа
   (режим управления «Делаю? (да/нет)», «Какой открыть?», список «какой
   файл?», экран телефона), приходит в ленте реплик с вариантами — те же,
   что у кнопок веба (ядро: BotInstance.cc_answer_options). Кнопка шлёт
   обычную реплику: номер варианта или «да»/«нет»/«отмена» — ядро разбирает
   её как набранную, с теми же проверками. Разбор строк-вариантов — как в
   web/src/sections/Chat.tsx (и _OPTION_LINE_RE в app/features/task_agent.py). */

import { lang } from '../i18n';

export interface AnswerOption {
  label?: string;
  send?: string;
  role?: 'yes' | 'no' | 'cancel';
}

export interface AnswerOptions {
  kind: 'options' | 'yesno' | 'continue';
  options: AnswerOption[];
}

const ROLES = new Set(['yes', 'no', 'cancel']);
const KINDS = new Set(['options', 'yesno', 'continue']);

/** Варианты из ленты; битое или пустое — null (кнопок нет). */
export function answerOptionsOf(v: unknown): AnswerOptions | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as { kind?: unknown; options?: unknown };
  if (typeof o.kind !== 'string' || !KINDS.has(o.kind) || !Array.isArray(o.options)) return null;
  const options: AnswerOption[] = [];
  for (const x of o.options) {
    if (!x || typeof x !== 'object') continue;
    const { label, send, role } = x as Record<string, unknown>;
    if (typeof role === 'string' && ROLES.has(role)) options.push({ role: role as AnswerOption['role'] });
    else if (typeof label === 'string' && label.trim() && typeof send === 'string' && send.trim())
      options.push({ label: label.trim(), send: send.trim() });
  }
  return options.length ? { kind: o.kind as AnswerOptions['kind'], options } : null;
}

// Слова кнопок «да/нет/бросить» — ядро понимает оба языка
const ROLE_SEND = {
  ru: { yes: 'да', no: 'нет', cancel: 'отмена' },
  en: { yes: 'yes', no: 'no', cancel: 'cancel' },
} as const;

/** Что уйдёт персоне по кнопке. */
export function answerSend(o: AnswerOption): string {
  return o.send ?? (o.role ? ROLE_SEND[lang][o.role] : '');
}

// Строка-вариант в вопросе: «- Пепперони — 549 ₽», «1. Кинопоиск — kinopoisk.ru»
const OPTION_LINE_RE = /^\s*(?:[-•*]|\d{1,2}[.)])\s+(.+?)\s*$/;

/** Текст вопроса без строк-вариантов, когда они стали кнопками; число не
 *  совпало с кнопками — как есть (лучше дубль, чем потерянный вариант). */
export function stripOptionLines(text: string, n: number): string {
  const lines = text.split('\n');
  if (!n || lines.filter((ln) => OPTION_LINE_RE.test(ln)).length !== n) return text;
  return lines
    .filter((ln) => !OPTION_LINE_RE.test(ln))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// Подсказка «(да/нет)», «(«да» / «отмена»)» в конце вопроса — её заменяют кнопки
const YESNO_HINT_RE = /\s*\(\s*«?(?:да|yes)»?\s*\/\s*«?(?:нет|отмена|no|cancel)»?\s*\)\s*$/i;

/** Вопрос да/нет без подсказки в скобках в конце (кнопки и так видны). */
export function stripYesNoHint(text: string): string {
  const out = text.replace(YESNO_HINT_RE, '').trim();
  return out || text;
}

// «Пепперони — 549 ₽», «Кинопоиск — kinopoisk.ru»: хвост после последнего
// « — » — то, чем варианты различаются (цена, адрес)
const TAIL_RE = /^(.*\S)\s+—\s+([^—]{1,24})$/;

/** Подпись варианта: [название, хвост] — хвост в стопке не режется. */
export function labelTail(label: string): [string, string | null] {
  const m = TAIL_RE.exec(label.trim());
  return m ? [m[1], m[2].trim()] : [label, null];
}

import { BASE_KIT } from './baseKit';
import { crc32 } from './localPersonas';
import { detectLanguage, languageName } from './language';
import type { OfflineKit } from './kit';

/* Краткое описание персоны для модели телефона на телефоне: тот же запрос
   карточки, что у ядра (app/features/short_prompt.py: CARD_PROMPT — в
   базовом ките приложения, card_messages, manual_prompt, clean_card), и
   правки, сделанные без ядра.

   Своё описание или составленное на телефоне ждёт ядра в очереди (KEY):
   getKit (kit.ts) накладывает его на кит персоны, так что Gemma получает его
   сразу; при связи personaSync.syncShortPrompts отдаёт его ядру (PUT
   short-prompt) и убирает из очереди. Составленное — с crc32 характера:
   характер сменился — карточка не накладывается, ядро её не примет (409).
   Без зависимостей приложения: scripts/test_offline_personas.py гоняет
   модуль под Node. */

export const CARD_MIN = 200; // символов: короче — модель не справилась
export const CARD_MAX = 2500; // длиннее — обрезается по абзацу
export const OWN_MAX = 4000; // своё описание — не длиннее (столько характера берёт телефон)
const MANUAL_JOINER = '\n\n---\n\n';

export interface CardMsg {
  role: 'system' | 'user';
  content: string;
}

/** Запрос карточки: системная часть с языком описания + характер. */
export function cardMessages(character: string): CardMsg[] {
  const name = languageName(detectLanguage(character));
  const lang = name
    ? `Write the card in ${name}, the language of the description.`
    : 'Write the card in the language of the description.';
  return [
    { role: 'system', content: `${BASE_KIT.card_prompt}\n\n${lang}` },
    { role: 'user', content: character },
  ];
}

/** Тот же запрос одним текстом — скопировать в любую нейросеть. */
export function manualPrompt(character: string): string {
  return character ? cardMessages(character).map((m) => m.content).join(MANUAL_JOINER) : '';
}

const SEPARATOR_RE = /^[\s\-─━═=_*~#]+$/;
const THINK_RE = /<think>[\s\S]*?<\/think>/gi;
const FENCE_RE = /^```[a-z]*\s*|\s*```$/g;

/** Ответ модели → карточка (как clean_card ядра): без рассуждений, обёртки
 * ``` и разделителей; длиннее CARD_MAX — по последнему абзацу, что влез.
 * Пусто — не вышло. */
export function cleanCard(text: string): string {
  let t = (text || '').replace(THINK_RE, '');
  t = t.trim().replace(FENCE_RE, '').trim();
  const lines = t
    .split('\n')
    .map((ln) => ln.replace(/\s+$/, ''))
    .filter((ln) => !SEPARATOR_RE.test(ln));
  t = lines.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  if (t.length > CARD_MAX) {
    const cut = t.slice(0, CARD_MAX);
    const para = cut.lastIndexOf('\n\n');
    const line = cut.lastIndexOf('\n');
    t = para >= CARD_MAX / 2 ? cut.slice(0, para) : line > 0 ? cut.slice(0, line) : cut;
  }
  return t.length >= CARD_MIN ? t : '';
}

// ── Очередь правок без ядра ───────────────────────────────────────────

// source yaml — своё (text пустой — убрать своё); auto — составлено на
// телефоне моделью из цепочки персоны (provider — кем)
export interface PendingShort {
  text: string;
  source: 'yaml' | 'auto';
  at: number; // мс
  crc?: number; // auto: crc32 характера, по которому составлено
  provider?: string;
}

const KEY = 'vpc-short-prompt-pending';
const listeners = new Set<() => void>();
let cachedRaw: string | null | undefined;
let cached: Record<string, PendingShort> = {};

export function pendingShorts(): Record<string, PendingShort> {
  let raw: string | null;
  try {
    raw = localStorage.getItem(KEY);
  } catch {
    return {};
  }
  if (raw === cachedRaw) return cached;
  cachedRaw = raw;
  try {
    const v = raw ? (JSON.parse(raw) as Record<string, PendingShort>) : {};
    cached = v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  } catch {
    cached = {};
  }
  return cached;
}

export function pendingShort(persona: string): PendingShort | null {
  return pendingShorts()[persona] ?? null;
}

function save(all: Record<string, PendingShort>) {
  try {
    if (Object.keys(all).length) localStorage.setItem(KEY, JSON.stringify(all));
    else localStorage.removeItem(KEY);
  } catch {
    /* хранилище недоступно — правка проживёт до перезапуска не дольше */
  }
  listeners.forEach((l) => l());
}

/** Правка без ядра (null — убрать из очереди). */
export function setPendingShort(persona: string, entry: PendingShort | null) {
  const all = { ...pendingShorts() };
  if (entry) all[persona] = entry;
  else if (persona in all) delete all[persona];
  else return;
  save(all);
}

/** Ядро приняло правку — из очереди, если её не сменили, пока она шла. */
export function settlePendingShort(persona: string, at: number) {
  if (pendingShort(persona)?.at === at) setPendingShort(persona, null);
}

/** id персоны сменился при переносе на ядро. */
export function movePendingShort(from: string, to: string) {
  const all = { ...pendingShorts() };
  if (!(from in all)) return;
  all[to] = all[from];
  delete all[from];
  save(all);
}

export function onPendingShortChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

const overlays = new WeakMap<OfflineKit, { p: PendingShort; out: OfflineKit }>();

/** Кит с правкой из очереди: своё — как поле short_prompt, составленное —
 * если характер тот же. Правки нет — тот же кит. */
export function withPendingShort(kit: OfflineKit): OfflineKit {
  const p = pendingShort(kit.persona);
  if (!p || kit.fallback) return kit;
  if (p.source === 'auto' && p.crc !== crc32(kit.parts.character)) return kit;
  const memo = overlays.get(kit);
  if (memo && memo.p === p) return memo.out;
  const text = p.text.trim();
  const out: OfflineKit = {
    ...kit,
    parts: { ...kit.parts, short: text },
    short_prompt: text
      ? { source: p.source, status: 'ready', at: p.at / 1000 }
      : { source: '', status: 'none', at: 0 },
  };
  overlays.set(kit, { p, out });
  return out;
}

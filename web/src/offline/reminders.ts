import { api, getApiUrl, isNativeApp } from '../api';
import { getLinkConfig } from '../link/client';
import { getKit, kitPersonas, onKitRefreshed, refreshKit } from './kit';
import type { OfflineKit } from './kit';
import { OfflineLlm } from './plugin';
import type { AlarmItem } from './plugin';
import { timeLine } from './context';
import {
  hasRemindWord,
  isPendingDecline,
  isRecurringAsk,
  isReminderCreateRequest,
  looksLikeTimeAttempt,
  parseReminder,
} from './remparse';

/* Напоминания без ноутбука.

   Будильник телефона (ReminderAlarms.java) знает все напоминания: ближайшие
   срабатывания напоминаний ядра (из кита каждой персоны) и поставленные на
   телефоне, пока ноутбука не было («local:…»). Ноутбук рядом — телефон даёт
   ему напомнить самому, нет — напоминает сам.

   Новое напоминание без ноутбука: время из фразы разбирает перенос
   парсера ядра (remparse.ts) — мгновенно и так же, как ядро; необычные
   фразы («в пятницу к обеду») — модель (облако или телефон) по заметке кита
   notes.extract, код проверяет результат. Времени нет — персона переспросит,
   задача ждёт ответа (как begin_pending_remind ядра). При связи с ноутбуком
   напоминание уходит в его ReminderManager (POST …/offline/reminders) и
   дальше живёт там. */

export interface LocalReminder {
  id: string;
  persona: string;
  task: string;
  at: number; // unix-секунды
  createdAt: number;
}

const LOCAL_KEY = 'vpc-offline-reminders';
const MAX_AHEAD_SEC = 366 * 86400;
const listeners = new Set<() => void>();

// Кит обновился — в нём свежие напоминания ядра: будильник заново
onKitRefreshed(() => void pushAlarms());

export function onRemindersChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function localReminders(): LocalReminder[] {
  try {
    const list = JSON.parse(localStorage.getItem(LOCAL_KEY) || '[]') as LocalReminder[];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function saveLocal(list: LocalReminder[]) {
  try {
    localStorage.setItem(LOCAL_KEY, JSON.stringify(list));
  } catch {
    /* хранилище недоступно */
  }
  listeners.forEach((l) => l());
}

function newId(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

/** Текст уведомления: «Напоминаю: …» на языке задачи. */
function alarmText(task: string | null | undefined): string {
  const t = (task || '').trim();
  const ru = !t || /[а-яё]/i.test(t);
  if (!t) return ru ? 'Время пришло — ты просил напомнить.' : "Time's up — you asked for a reminder.";
  return ru ? `Напоминаю: ${t}` : `Reminder: ${t}`;
}

/** Адреса ноутбука для пробы будильника: «host:port». */
function probes(): string[] {
  const link = getLinkConfig();
  if (link) return (link.l || []).slice(0, 3);
  try {
    const u = new URL(getApiUrl());
    return [`${u.hostname}:${u.port || (u.protocol === 'https:' ? '443' : '80')}`];
  } catch {
    return [];
  }
}

/** Отдать будильнику телефона полный список напоминаний всех персон. */
export async function pushAlarms(): Promise<void> {
  if (!isNativeApp()) return;
  const now = Date.now() / 1000;
  const items: AlarmItem[] = [];
  for (const persona of kitPersonas()) {
    const kit = getKit(persona);
    for (const r of kit?.reminders ?? []) {
      if (r.at > now) items.push({ ref: r.ref, persona, name: kit!.name, text: alarmText(r.task), at: r.at });
    }
  }
  for (const r of localReminders()) {
    if (r.at <= now) continue;
    items.push({ ref: `local:${r.id}`, persona: r.persona, name: getKit(r.persona)?.name ?? r.persona, text: alarmText(r.task), at: r.at });
  }
  try {
    await OfflineLlm.syncReminders({ items, probes: probes() });
  } catch {
    /* старый APK без будильников — без них */
  }
  listeners.forEach((l) => l());
}

export function addLocalReminder(persona: string, task: string, at: number): LocalReminder {
  const r: LocalReminder = { id: newId(), persona, task, at, createdAt: Date.now() / 1000 };
  saveLocal([...localReminders(), r]);
  void pushAlarms();
  return r;
}

export function cancelLocalReminder(id: string) {
  saveLocal(localReminders().filter((r) => r.id !== id));
  void pushAlarms();
}

/** Напоминания ядра и телефона для промпта: «- 11.10 09:00 — задача». */
export function scheduledNote(kit: OfflineKit): string {
  const now = Date.now() / 1000;
  const all = [
    ...(kit.reminders ?? []).map((r) => ({ at: r.at, task: r.task || '' })),
    ...localReminders().filter((r) => r.persona === kit.persona).map((r) => ({ at: r.at, task: r.task })),
  ]
    .filter((r) => r.at > now)
    .sort((a, b) => a.at - b.at)
    .slice(0, 8);
  if (!all.length || !kit.notes?.scheduled) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  const list = all
    .map((r) => {
      const d = new Date(r.at * 1000);
      return `- ${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())} — ${r.task || '(no task)'}`;
    })
    .join('\n');
  return kit.notes.scheduled.replace('{list}', list);
}

/** Похоже на просьбу о напоминании — как _has_remind_word ядра. */
export const looksLikeReminder = hasRemindWord;

export interface ReminderPlan {
  note: string; // заметка для ответа персоны
  reminder?: LocalReminder; // поставлено
}

// Заметки кита до версии 3 — без «не понял время» и «передумал»
const NOTE_DEFAULTS = {
  not_understood:
    'The user is answering the question about the time for the reminder{task}, but the time could not be ' +
    'understood. Ask again: how soon to remind (for example, "in 2 hours", "tomorrow at 12"). In your own ' +
    'style, briefly. Do NOT confirm that any reminder was scheduled — it was NOT.',
  declined:
    'The user decided not to set the reminder{task}. Nothing was scheduled. Acknowledge briefly in your own ' +
    'style. Do NOT say any reminder was set.',
};

const note = (kit: OfflineKit, key: keyof typeof NOTE_DEFAULTS) => kit.notes[key] || NOTE_DEFAULTS[key];
const quoted = (task: string) => (task ? ` '${task}'` : '');

// Вопрос «когда напомнить?» висит: задача ждёт времени (как у ядра — 10 минут)
const PENDING_KEY = 'vpc-offline-remind-pending';
const PENDING_TTL_SEC = 600;

function pendingAll(): Record<string, { task: string; until: number }> {
  try {
    const o = JSON.parse(localStorage.getItem(PENDING_KEY) || '{}') as Record<string, { task: string; until: number }>;
    return o && typeof o === 'object' ? o : {};
  } catch {
    return {};
  }
}

function setPending(persona: string, task: string | null) {
  const all = pendingAll();
  if (task === null) delete all[persona];
  else all[persona] = { task, until: Date.now() / 1000 + PENDING_TTL_SEC };
  try {
    localStorage.setItem(PENDING_KEY, JSON.stringify(all));
  } catch {
    /* хранилище недоступно */
  }
}

function getPending(persona: string): { task: string } | null {
  const p = pendingAll()[persona];
  return p && p.until > Date.now() / 1000 ? p : null;
}

const collapseTask = (task: string | null) => (task ?? '').replace(/\s+/g, ' ').trim();

/** Обращения к персоне в начале фразы («Коннор, напомни…») — срезать из задачи. */
function names(kit: OfflineKit): string[] {
  return [...new Set([kit.name, kit.persona, 'коннор', 'жабка', 'arrodes', 'connor', 'арродес'].filter(Boolean).map((n) => n.toLowerCase()))];
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`;

/** Когда напомнить — для заметки персоне. Модель телефона складывает время
 * с ошибками, поэтому считать ей нечего: до часа — «in 3 minutes» (как
 * format_delay ядра), в пределах суток — «at 19:00 (in 2 hours)», дальше —
 * дата и время. Точное время человек видит в карточке ⏰. */
export function whenText(at: number, now: number = Date.now() / 1000): string {
  const d = new Date(at * 1000);
  const sec = Math.round(at - now);
  if (sec >= 86400) return timeLine(d);
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  if (!h) return `in ${plural(Math.max(1, m), 'minute')}`;
  const span = `${plural(h, 'hour')}${m && m < 60 ? ` ${plural(m, 'minute')}` : ''}`;
  return `at ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')} (in ${span})`;
}

/** Поставить на время at (unix-секунды) или сказать, что оно прошло. */
function scheduleAt(kit: OfflineKit, task: string, at: number): ReminderPlan {
  const n = kit.notes;
  if (at <= Date.now() / 1000 + 20) return { note: n.past.replace('{task}', quoted(task)) };
  const reminder = addLocalReminder(kit.persona, task, at);
  return { note: n.set.replace('{task}', quoted(task)).replace('{when}', whenText(at)), reminder };
}

/** Время не названо: персона переспросит, задача ждёт ответа. */
export function askTimePlan(kit: OfflineKit, task: string): ReminderPlan {
  setPending(kit.persona, task);
  return { note: kit.notes.ask_time.replace('{task}', quoted(task)) };
}

/**
 * Реплика без ноутбука и напоминания — по правилам ядра (_reminder_turn):
 * поставлено, переспросить или отказ — заметка персоне; 'model' — явная
 * просьба, но время не разобрано: разберёт модель; null — не о напоминании.
 */
export function planFromText(kit: OfflineKit, text: string, now: Date = new Date()): ReminderPlan | 'model' | null {
  const nowSec = now.getTime() / 1000;
  const pending = getPending(kit.persona);
  const fresh = isReminderCreateRequest(text);
  if (pending && !fresh) {
    if (isPendingDecline(text)) {
      setPending(kit.persona, null);
      return { note: note(kit, 'declined').replace('{task}', quoted(pending.task)) };
    }
    const p = parseReminder(`напомни ${text}`, names(kit), now) ?? parseReminder(`напомни в ${text}`, names(kit), now);
    if (p) {
      setPending(kit.persona, null);
      return scheduleAt(kit, pending.task, nowSec + p.delay);
    }
    if (looksLikeTimeAttempt(text)) return { note: note(kit, 'not_understood').replace('{task}', quoted(pending.task)) };
    setPending(kit.persona, null); // посторонняя реплика — вопрос снят
  }
  if (!hasRemindWord(text)) return null;
  const parsed = parseReminder(text, names(kit), now);
  if (isRecurringAsk(text)) return { note: kit.notes.recurring.replace('{task}', quoted(parsed?.task ?? '')) };
  if (parsed) {
    setPending(kit.persona, null);
    return scheduleAt(kit, collapseTask(parsed.task), nowSec + parsed.delay);
  }
  // «напоминание пришло вовремя», «ты мне напомнил?» — разговор, не просьба
  return fresh ? 'model' : null;
}

/** Разобрать ответ модели на notes.extract. */
export function parseExtraction(raw: string): { task: string; at: string | null; recurring: boolean } | null {
  const m = /\{[\s\S]*\}/.exec(raw.replace(/<think>[\s\S]*?<\/think>/gi, ''));
  if (!m) return null;
  try {
    const o = JSON.parse(m[0]) as { task?: unknown; at?: unknown; recurring?: unknown };
    return {
      task: typeof o.task === 'string' ? o.task.trim() : '',
      at: typeof o.at === 'string' && o.at.trim() ? o.at.trim() : null,
      recurring: o.recurring === true,
    };
  } catch {
    return null;
  }
}

/** «2026-10-11T09:00» — местное время телефона → unix-секунды. */
export function parseLocalTime(at: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})/.exec(at);
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], 0, 0);
  return Number.isNaN(d.getTime()) ? null : d.getTime() / 1000;
}

/** Что сказать персоне и что поставить — по просьбе, разобранной моделью. */
export function planReminder(
  kit: OfflineKit,
  ex: { task: string; at: string | null; recurring: boolean },
): ReminderPlan {
  if (ex.recurring) return { note: kit.notes.recurring.replace('{task}', quoted(ex.task)) };
  const at = ex.at ? parseLocalTime(ex.at) : null;
  if (at === null || at > Date.now() / 1000 + MAX_AHEAD_SEC) return askTimePlan(kit, ex.task);
  return scheduleAt(kit, ex.task, at);
}

/** Заметка для модели, разбирающей просьбу: местные дата и время сейчас. */
export function extractSystem(kit: OfflineKit): string {
  const off = -new Date().getTimezoneOffset();
  const tz = `UTC${off >= 0 ? '+' : '-'}${String(Math.floor(Math.abs(off) / 60)).padStart(2, '0')}:${String(Math.abs(off) % 60).padStart(2, '0')}`;
  return kit.notes.extract.replace('{now}', `${timeLine()} (${tz})`);
}

let syncing: Promise<void> | null = null;

/** Ноутбук вернулся: поставленные без него напоминания — ему, будильник — заново. */
export function syncLocalReminders(): Promise<void> {
  if (syncing) return syncing;
  syncing = (async () => {
    const byPersona = new Map<string, LocalReminder[]>();
    for (const r of localReminders()) byPersona.set(r.persona, [...(byPersona.get(r.persona) ?? []), r]);
    for (const [persona, list] of byPersona) {
      try {
        const res = await api.offlineReminders(
          persona,
          list.map((r) => ({ id: r.id, task: r.task, at: r.at })),
        );
        if (!res.enabled) continue; // у персоны напоминания выключены — будильник телефона остаётся
        const done = new Set([...Object.keys(res.created), ...res.past]);
        saveLocal(localReminders().filter((r) => !done.has(r.id)));
        await refreshKit(persona); // в ките — уже напоминания ядра
      } catch {
        /* не ушло — при следующей связи */
      }
    }
    await pushAlarms();
  })().finally(() => {
    syncing = null;
  });
  return syncing;
}

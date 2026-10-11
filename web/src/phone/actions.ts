import { Capacitor, registerPlugin } from '@capacitor/core';
import { isNativeApp } from '../api';
import { pluginCode } from '../offline/plugin';
import { alarmDayFits, isDecline, matchApp, parseAlarmAnswer, parseChoice, parsePhoneCommand, parseTimerAnswer } from './parse';
import type { AppEntry, ClockTime, DaySpec, PhoneCommand } from './parse';
import type { PhoneActionReport, PhoneActionStatus } from './notes';

/* Команды телефону из чата: «поставь будильник на 8», «таймер на 10 минут»,
   «покажи будильники», «открой Telegram», «включи фонарик». Разбор — parse.ts,
   исполнение — плагин PhoneActions (Java): будильник и таймер ставят системные
   «Часы» без своего экрана, приложение открывается по названию из списка
   установленных. Персоне уходит только итог (PhoneActionReport): отвечает она
   по нему и ничего не «ставит» на словах. Список приложений с телефона не
   уходит — только название открытого или варианты на выбор.

   Журнал (phoneLog) — для проверки в «Настройках» (PhoneCheckCard): что из
   похожего на команду разобрано, чем кончилось, а что не исполнялось и
   почему (нет плагина в сборке, режим управления…). Хранится на телефоне. */

interface PhoneActionsPlugin {
  setAlarm(opts: { hour: number; minute: number; days?: number[]; label?: string }): Promise<{ handler: string }>;
  setTimer(opts: { seconds: number; label?: string }): Promise<{ handler: string }>;
  showAlarms(): Promise<{ handler: string }>;
  listApps(): Promise<{ apps: AppEntry[] }>;
  openApp(opts: { package: string }): Promise<void>;
  torch(opts: { on: boolean }): Promise<void>;
  status(): Promise<PhoneStatus>;
}

export interface PhoneHandler {
  package: string;
  label: string;
}

/** Что умеет телефон: кто примет будильник, таймер, список; фонарик; приложения. */
export interface PhoneStatus {
  alarm: PhoneHandler | null;
  timer: PhoneHandler | null;
  alarms: PhoneHandler | null;
  torch: boolean;
  apps: number;
  device: string;
  sdk: number;
}

const PhoneActions = registerPlugin<PhoneActionsPlugin>('PhoneActions');

/** Команды телефону возможны: приложение на Android со свежим плагином. */
export function phoneActionsAvailable(): boolean {
  return isNativeApp() && Capacitor.isPluginAvailable('PhoneActions');
}

export function phoneStatus(): Promise<PhoneStatus> {
  return PhoneActions.status();
}

// ─── Журнал ───

/** Запись журнала: result — status итога, или почему не исполнялось:
 * not_command (похоже на команду, но не разобрано), not_app (не приложение —
 * обычная реплика), declined («не надо» на переспрос), error, или причина
 * пропуска чатом (PhoneSkip). */
export interface PhoneLogEntry {
  ts: number;
  text: string;
  kind?: string;
  result: string;
  detail?: string;
}

export type PhoneSkip = 'no_plugin' | 'control_mode' | 'skin' | 'image';

const LOG_KEY = 'vpc-phone-log';
const LOG_MAX = 20;
const logListeners = new Set<() => void>();

export function phoneLog(): PhoneLogEntry[] {
  try {
    const v = JSON.parse(localStorage.getItem(LOG_KEY) || '[]') as PhoneLogEntry[];
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function addLog(e: Omit<PhoneLogEntry, 'ts'>) {
  const list = [{ ts: Date.now(), ...e, text: e.text.slice(0, 120) }, ...phoneLog()].slice(0, LOG_MAX);
  try {
    localStorage.setItem(LOG_KEY, JSON.stringify(list));
  } catch {
    /* хранилище недоступно — журнал не ведётся */
  }
  for (const fn of logListeners) fn();
}

export function clearPhoneLog() {
  try {
    localStorage.removeItem(LOG_KEY);
  } catch {
    /* хранилище недоступно */
  }
  for (const fn of logListeners) fn();
}

export function onPhoneLogChange(fn: () => void): () => void {
  logListeners.add(fn);
  return () => {
    logListeners.delete(fn);
  };
}

// Похоже на команду, хоть и не разобрано: в журнал — чтобы было видно, что
// фраза дошла, но не понята
const HINT_RE = /будильник|таймер|засек|фонар|разбуди|открой|откройте|запусти|alarm|timer|flashlight|torch|wake me|\bopen\b|\blaunch\b/iu;

/** Чат не стал исполнять реплику (нет плагина, режим управления, скин,
 * картинка) — в журнал, если она похожа на команду. */
export function notePhoneSkip(text: string, reason: PhoneSkip, names: string[] = []) {
  if (!text) return;
  const cmd = parsePhoneCommand(text, names);
  if (cmd || HINT_RE.test(text)) addLog({ text, kind: cmd?.kind, result: reason });
}

// Что сказал плагин в последнем вызове: кто принял интент или код отказа
let lastDetail = '';

async function call<T>(fn: () => Promise<T>): Promise<T> {
  try {
    const r = await fn();
    const handler = (r as { handler?: string } | undefined)?.handler;
    if (handler) lastDetail = handler;
    return r;
  } catch (e) {
    lastDetail = `${pluginCode(e) || 'error'}: ${e instanceof Error ? e.message : String(e)}`;
    throw e;
  }
}

// Переспрос висит («на какое время?», «какое из двух?»): следующая реплика —
// ответ на него. Посторонняя реплика или «не надо» — вопрос снят
type Pending =
  | { kind: 'alarm'; days: number[]; day: DaySpec | null; label: string }
  | { kind: 'timer'; label: string }
  | { kind: 'app'; apps: AppEntry[] };

const PENDING_TTL_MS = 5 * 60_000;
const pending = new Map<string, { p: Pending; until: number }>();

function takePending(persona: string): Pending | null {
  const e = pending.get(persona);
  pending.delete(persona);
  return e && e.until > Date.now() ? e.p : null;
}

const setPending = (persona: string, p: Pending) => pending.set(persona, { p, until: Date.now() + PENDING_TTL_MS });

// Список приложений: свежий раз в минуту (поставили новое — найдётся)
const APPS_TTL_MS = 60_000;
let appsCache: { at: number; apps: AppEntry[] } | null = null;

async function installedApps(fresh = false): Promise<AppEntry[]> {
  if (!fresh && appsCache && Date.now() - appsCache.at < APPS_TTL_MS) return appsCache.apps;
  const { apps } = await call(() => PhoneActions.listApps());
  appsCache = { at: Date.now(), apps };
  return apps;
}

function failStatus(e: unknown): PhoneActionStatus {
  const code = pluginCode(e);
  return code === 'no_handler' || code === 'busy' ? code : 'failed';
}

async function setAlarm(time: ClockTime, days: number[], day: DaySpec | null, label: string, now: Date): Promise<PhoneActionReport> {
  const base: PhoneActionReport = {
    kind: 'alarm',
    status: 'ok',
    hour: time.hour,
    minute: time.minute,
    ...(days.length ? { days } : {}),
    ...(label ? { label } : {}),
  };
  if (!days.length) {
    const fit = alarmDayFits(time, day, now);
    if (!fit.fits) return { ...base, status: 'unsupported_date', nearest: fit.nearest };
  }
  try {
    await call(() => PhoneActions.setAlarm({ hour: time.hour, minute: time.minute, days, label }));
    return base;
  } catch (e) {
    return { ...base, status: failStatus(e) };
  }
}

/** «Через 20 минут» → время будильника (до минуты вверх) и его день. */
function relativeAlarm(seconds: number, now: Date): { time: ClockTime; day: DaySpec } {
  const at = new Date(Math.ceil((now.getTime() + seconds * 1000) / 60_000) * 60_000);
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const offset = Math.round((startOf(at) - startOf(now)) / 86_400_000);
  return { time: { hour: at.getHours(), minute: at.getMinutes() }, day: { offset } };
}

async function startTimer(seconds: number, label: string): Promise<PhoneActionReport> {
  const base: PhoneActionReport = { kind: 'timer', status: 'ok', seconds, ...(label ? { label } : {}) };
  try {
    await call(() => PhoneActions.setTimer({ seconds, label }));
    return base;
  } catch (e) {
    return { ...base, status: failStatus(e) };
  }
}

async function showAlarms(kind: 'alarms_list' | 'alarm_off'): Promise<PhoneActionReport> {
  try {
    await call(() => PhoneActions.showAlarms());
    return { kind, status: 'ok' };
  } catch (e) {
    return { kind, status: failStatus(e) };
  }
}

async function openApp(app: AppEntry): Promise<PhoneActionReport> {
  try {
    await call(() => PhoneActions.openApp({ package: app.package }));
    lastDetail = app.package;
    return { kind: 'open_app', status: 'ok', app: app.label };
  } catch (e) {
    // Удалили между списком и запуском — нет точки входа
    return { kind: 'open_app', status: pluginCode(e) === 'not_found' ? 'not_found' : 'failed', app: app.label };
  }
}

async function execute(persona: string, cmd: PhoneCommand, now: Date): Promise<PhoneActionReport | null> {
  switch (cmd.kind) {
    case 'torch':
      try {
        await call(() => PhoneActions.torch({ on: cmd.on }));
        return { kind: 'torch', status: 'ok', on: cmd.on };
      } catch (e) {
        return { kind: 'torch', status: failStatus(e), on: cmd.on };
      }
    case 'alarms_list':
    case 'alarm_off':
      return showAlarms(cmd.kind);
    case 'timer':
      if (!cmd.seconds) {
        setPending(persona, { kind: 'timer', label: cmd.label });
        return { kind: 'timer', status: 'ask' };
      }
      return startTimer(cmd.seconds, cmd.label);
    case 'alarm':
      if (cmd.relative) {
        const r = relativeAlarm(cmd.relative, now);
        return setAlarm(r.time, [], r.day, cmd.label, now);
      }
      if (!cmd.time) {
        setPending(persona, { kind: 'alarm', days: cmd.days, day: cmd.day, label: cmd.label });
        return { kind: 'alarm', status: 'ask' };
      }
      return setAlarm(cmd.time, cmd.days, cmd.day, cmd.label, now);
    case 'open_app': {
      let apps: AppEntry[];
      try {
        apps = await installedApps();
      } catch {
        return cmd.weak ? null : { kind: 'open_app', status: 'failed', app: cmd.name };
      }
      let found = matchApp(cmd.name, apps, cmd.weak);
      if (found.status === 'not_found' && appsCache && Date.now() - appsCache.at > 2000) {
        // Приложение могли поставить только что — список заново
        found = matchApp(cmd.name, await installedApps(true).catch(() => apps), cmd.weak);
      }
      // «включи X» без точного бренда — обычная реплика («включи музыку»)
      if (cmd.weak && found.status !== 'ok') return null;
      if (found.status === 'ok') return openApp(found.app);
      if (found.status === 'ambiguous') {
        setPending(persona, { kind: 'app', apps: found.apps });
        return { kind: 'open_app', status: 'ambiguous', app: cmd.name, candidates: found.apps.map((a) => a.label) };
      }
      // Не нашлось: честно сказать — если просили именно приложение;
      // «открой X» без явного приложения — обычная реплика
      return cmd.explicit ? { kind: 'open_app', status: 'not_found', app: cmd.name } : null;
    }
  }
}

/** Ответ на висящий переспрос: итог; null — «не надо» (обычная реплика);
 * undefined — это не ответ, реплика разбирается заново. */
async function answerPending(p: Pending, text: string, now: Date): Promise<PhoneActionReport | null | undefined> {
  if (isDecline(text)) return null;
  if (p.kind === 'alarm') {
    const a = parseAlarmAnswer(text);
    if (!a) return undefined;
    if (a.relative) {
      const r = relativeAlarm(a.relative, now);
      return setAlarm(r.time, [], r.day, p.label, now);
    }
    return a.time ? setAlarm(a.time, p.days, p.day, p.label, now) : undefined;
  }
  if (p.kind === 'timer') {
    const s = parseTimerAnswer(text);
    return s ? startTimer(s, p.label) : undefined;
  }
  const i = parseChoice(text, p.apps.length);
  if (i !== null) return openApp(p.apps[i]);
  const byName = matchApp(text, p.apps);
  return byName.status === 'ok' ? openApp(byName.app) : undefined;
}

/**
 * Реплика — команда телефону? Исполнить и вернуть итог для персоны; null —
 * обычная реплика. names — обращения к персоне («Коннор, поставь…»).
 * Не бросает: сбой плагина — итог со status failed.
 */
export async function runPhoneCommand(persona: string, text: string, names: string[] = [], now: Date = new Date()): Promise<PhoneActionReport | null> {
  lastDetail = '';
  try {
    const p = takePending(persona);
    const cmd = parsePhoneCommand(text, names);
    if (p && !cmd) {
      const r = await answerPending(p, text, now);
      if (r !== undefined) {
        addLog({ text, kind: r?.kind ?? p.kind, result: r ? r.status : 'declined', detail: lastDetail || undefined });
        return r;
      }
    }
    if (!cmd) {
      if (HINT_RE.test(text)) addLog({ text, result: 'not_command' });
      return null;
    }
    const r = await execute(persona, cmd, now);
    addLog({ text, kind: cmd.kind, result: r ? r.status : 'not_app', detail: lastDetail || undefined });
    return r;
  } catch (e) {
    addLog({ text, result: 'error', detail: e instanceof Error ? e.message : String(e) });
    return null;
  }
}

/** Разбор фразы для проверки в «Настройках» — без исполнения; для «открой X» —
 * какое приложение нашлось бы. */
export async function explainPhrase(text: string): Promise<{ cmd: PhoneCommand | null; app?: string; candidates?: string[]; appsError?: string }> {
  const cmd = parsePhoneCommand(text);
  if (cmd?.kind !== 'open_app') return { cmd };
  try {
    const found = matchApp(cmd.name, await installedApps(true), cmd.weak);
    if (found.status === 'ok') return { cmd, app: found.app.label };
    if (found.status === 'ambiguous') return { cmd, candidates: found.apps.map((a) => a.label) };
    return { cmd };
  } catch (e) {
    return { cmd, appsError: e instanceof Error ? e.message : String(e) };
  }
}

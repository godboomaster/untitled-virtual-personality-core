import { registerPlugin } from '@capacitor/core';
import type { PluginListenerHandle } from '@capacitor/core';
import { api, isNativeApp } from '../api';
import type { ServiceCheck, ServiceFeedItem, ServiceRule, ServiceState } from '../api';
import { isStandalone } from '../standalone';
import { addOfflineMessage, offlineMessages } from './store';

/* «Сервисы» на телефоне без компьютера: ленты RSS, GitHub, почта IMAP и
   Gmail проверяет сам телефон — нативная часть (PhoneServices.java): в фоне
   раз в 15 или 30 минут (JobScheduler), разбор правилами и признаками без
   модели, текст — шаблоном ядра. WebView в фоне замерзает, поэтому веб здесь
   только настраивает и показывает; ключи доступа хранит плагин (Android
   Keystore), вебу — маски.

   Сообщение «Сервисов» — реплика персоны: плагин показывает уведомление и
   кладёт сообщение в очередь, а веб при открытии забирает очередь в
   переписку без ядра (store.ts, source notify) — так оно появляется в чате
   и уходит на компьютер вместе с перепиской.

   Работают, только пока выбран режим «без компьютера» (setMode). Появился
   компьютер — один раз спросить, перенести ли сервисы туда
   (useServicesOffer.ts → POST /api/integrations/import): ключи, курсоры
   («что уже видел»), правила, лента. Перенесённое телефон больше не
   проверяет (markMoved) — главный теперь компьютер. */

export type PhoneSource = 'rss' | 'github' | 'imap' | 'gmail';
export const PHONE_SOURCES: PhoneSource[] = ['rss', 'github', 'imap', 'gmail'];

export interface PhoneNotify {
  persona: string | null;
  quiet: { enabled: boolean; from: string; to: string; urgent: boolean };
  digest_times: string[];
  max_per_hour: number;
  language: 'auto' | 'ru' | 'en';
}

export interface PhoneSettings {
  notify: PhoneNotify;
  services: {
    rss: { enabled: boolean; feeds: { url: string; title: string }[] };
    github: { enabled: boolean };
    imap: { enabled: boolean };
    gmail: { enabled: boolean };
  };
  rules: ServiceRule[];
  dnd_until: number;
  interval_min: 15 | 30;
  moved: Partial<Record<PhoneSource, number>>; // перенесено на компьютер: когда
}

export interface PhoneServicesStatus {
  standalone: boolean;
  running: boolean;
  scheduled: boolean;
  settings: PhoneSettings;
  personas: { id: string; name: string }[];
  persona: { id: string; name: string } | null;
  notificationsEnabled: boolean;
  accounts: {
    github: { connected: boolean; login: string; token_hint: string };
    imap: { connected: boolean; user: string; host: string; port: number; folder: string };
    google: { client: boolean; client_id: string; connected: boolean; email: string; gmail: boolean };
  };
  status: Record<PhoneSource, ServiceState & { active: boolean }>;
  last_run: number;
  counts: { queued: number; digest: number; today: number; notified_today: number };
  feed: Omit<ServiceFeedItem, 'feedback'>[];
  movable: PhoneSource[];
  now: number;
  debuggable: boolean;
}

export interface CheckNowResult {
  ok: boolean;
  busy?: boolean;
  error?: string;
  polled?: Partial<Record<PhoneSource, { ok: boolean; new?: number; error?: string; auth?: boolean }>>;
  new?: number;
  delivered?: number;
}

interface Delivered {
  id: string;
  persona: string;
  name: string;
  text: string;
  ts: number;
  kind: string;
}

// Тело POST /api/integrations/import (SvcTransfer.java); sources — что в нём
export interface PhoneServicesBundle {
  sources: PhoneSource[];
  [key: string]: unknown;
}

interface PhoneServicesPlugin {
  status(): Promise<PhoneServicesStatus>;
  setMode(opts: { standalone: boolean; personas?: { id: string; name: string }[]; lang?: string }): Promise<void>;
  updateSettings(patch: {
    notify?: Partial<PhoneNotify>;
    services?: Record<string, Record<string, unknown>>;
    interval_min?: number;
  }): Promise<void>;
  addRule(rule: { action: ServiceRule['action']; source: string; match: string }): Promise<ServiceRule>;
  deleteRule(opts: { id: string }): Promise<void>;
  setDnd(opts: { minutes: number }): Promise<void>;
  checkRss(opts: { url: string }): Promise<{ url: string; ok: boolean; title?: string; items?: number; error?: string }>;
  connectGithub(opts: { token: string }): Promise<{ ok: boolean; login?: string; error?: string }>;
  connectImap(opts: { user: string; password: string; host?: string; port?: number; folder?: string }): Promise<{
    ok: boolean;
    host?: string;
    error?: string;
  }>;
  imapPreset(opts: { user: string }): Promise<{ host: string; port: number }>;
  setGoogleClient(opts: { client_id?: string; client_secret?: string; json_text?: string }): Promise<void>;
  googleLogin(): Promise<{ url: string }>;
  googleLoginResult(): Promise<{ result: { ok: boolean; error?: string; email?: string } | null; connected: boolean; email: string }>;
  googleFinish(opts: { url: string }): Promise<{ ok: boolean; error?: string; email?: string }>;
  disconnect(opts: { account: 'rss' | 'github' | 'imap' | 'google' }): Promise<void>;
  checkService(opts: { service: 'github' | 'imap' | 'gmail' }): Promise<ServiceCheck>;
  checkNow(opts?: { service?: PhoneSource }): Promise<CheckNowResult>;
  digestNow(): Promise<{ sent: number }>;
  testNotify(): Promise<{ ok: boolean }>;
  clearFeed(): Promise<void>;
  takeDelivered(): Promise<{ items: Delivered[] }>;
  exportForCore(): Promise<PhoneServicesBundle>;
  markMoved(opts: { services: PhoneSource[] }): Promise<void>;
  resume(opts: { service: PhoneSource }): Promise<void>;
  requestNotificationPermission(): Promise<{ granted: boolean }>;
  setTestEndpoints(opts: { github?: string; token?: string; gmail?: string; auth?: string }): Promise<void>;
  addListener(event: 'delivered' | 'changed', cb: () => void): Promise<PluginListenerHandle>;
}

export const PhoneServices = registerPlugin<PhoneServicesPlugin>('PhoneServices');

// ── Режим и персоны ───────────────────────────────────────────────────

/** Персоны для «кто сообщает»: последняя, с кем общались, — первой (как у ядра). */
function byRecent(personas: { id: string; name: string }[]): { id: string; name: string }[] {
  const last = (id: string) => {
    const list = offlineMessages(id);
    return list.length ? list[list.length - 1].ts : 0;
  };
  return [...personas].sort((a, b) => last(b.id) - last(a.id));
}

/** Сказать плагину, работать ли (только без компьютера), и кто может сообщать. */
export function syncPhoneServicesMode(personas: { id: string; name: string }[], lang: string): void {
  if (!isNativeApp()) return;
  void PhoneServices.setMode({ standalone: isStandalone(), personas: byRecent(personas), lang }).catch(() => {});
}

/** «Подключить компьютер»: телефон перестаёт проверять сервисы сразу. */
export function stopPhoneServices(): Promise<void> {
  if (!isNativeApp()) return Promise.resolve();
  return PhoneServices.setMode({ standalone: false }).catch(() => {});
}

// ── Доставленное — в чат ──────────────────────────────────────────────

let draining: Promise<number> | null = null;

/** Забрать очередь плагина в переписку без ядра. → сколько сообщений. */
export function drainDelivered(): Promise<number> {
  if (!isNativeApp()) return Promise.resolve(0);
  if (draining) return draining;
  draining = PhoneServices.takeDelivered()
    .then(({ items }) => {
      for (const it of items) {
        if (!it.persona || !it.text) continue;
        addOfflineMessage(it.persona, { role: 'assistant', text: it.text, source: 'notify', ts: it.ts });
      }
      return items.length;
    })
    .catch(() => 0)
    .finally(() => {
      draining = null;
    });
  return draining;
}

let started = false;

/** Запуск приложения: очередь — сразу, при каждом возврате на экран и по событию плагина. */
export function initPhoneServices(): void {
  if (!isNativeApp() || started) return;
  started = true;
  void drainDelivered();
  void PhoneServices.addListener('delivered', () => void drainDelivered()).catch(() => {});
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void drainDelivered();
  });
}

// ── Перенос на компьютер ──────────────────────────────────────────────

const OFFER_KEY = 'vpc-phone-services-offer';

function signature(st: PhoneServicesStatus): string {
  const a = st.accounts;
  return st.movable
    .map((s) =>
      s === 'rss'
        ? `rss:${st.settings.services.rss.feeds.map((f) => f.url).join(',')}`
        : s === 'github'
          ? `github:${a.github.login}:${a.github.token_hint}`
          : s === 'imap'
            ? `imap:${a.imap.user}@${a.imap.host}`
            : `gmail:${a.google.email}`,
    )
    .sort()
    .join('|');
}

/** Сервисы телефона, о переносе которых ещё не спрашивали; null — спрашивать не о чем. */
export async function servicesToOffer(): Promise<PhoneServicesStatus | null> {
  if (!isNativeApp()) return null;
  const st = await PhoneServices.status().catch(() => null);
  if (!st || !st.movable.length) return null;
  try {
    if (localStorage.getItem(OFFER_KEY) === signature(st)) return null;
  } catch {
    /* хранилище недоступно — спросим */
  }
  return st;
}

/** Ответ на вопрос о переносе — этот набор больше не предлагать. */
export function rememberServicesOffer(st: PhoneServicesStatus) {
  try {
    localStorage.setItem(OFFER_KEY, signature(st));
  } catch {
    /* хранилище недоступно — спросит ещё раз */
  }
}

export interface ServicesMoved {
  moved: PhoneSource[]; // теперь их проверяет компьютер
  skipped: Partial<Record<PhoneSource, string>>; // на компьютере уже свой аккаунт — главный он
  rules: number;
  items: number;
}

/** Перенести сервисы телефона на ядро; перенесённое и пропущенное телефон больше не проверяет. */
export async function moveServicesToCore(): Promise<ServicesMoved> {
  const bundle = await PhoneServices.exportForCore();
  const res = await api.importPhoneServices(bundle);
  // Пропущенное (на компьютере свой аккаунт) — тоже прочь с телефона: главный компьютер
  const done = [...res.moved, ...Object.keys(res.skipped)].filter((s): s is PhoneSource =>
    (PHONE_SOURCES as string[]).includes(s),
  );
  if (done.length) await PhoneServices.markMoved({ services: done });
  return {
    moved: res.moved.filter((s): s is PhoneSource => (PHONE_SOURCES as string[]).includes(s)),
    skipped: res.skipped as ServicesMoved['skipped'],
    rules: res.rules,
    items: res.items,
  };
}

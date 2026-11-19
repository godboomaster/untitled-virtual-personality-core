import { useEffect, useReducer } from 'react';
import { registerPlugin } from '@capacitor/core';
import { pluginCode } from './plugin';
import { enabledSites, fitPrompt, WEBCHAT_SITES } from './webchatSites';
import type { PromptMsg, WebchatId, WebchatPrefs, WebchatSite } from './webchatSites';

/* Веб-чаты без ключей: звено ответа без ядра (engine.ts) — по умолчанию
   между облаком по ключу и Gemma, своё место — в «Порядке ответа» (chain.ts).
   Сайт открывает невидимый WebView в своём профиле (WebChatPlugin.java).
   Какие сайты включены — выбирает человек во вкладке «Провайдеры»; по
   умолчанию все выключены: переписка уходит на сторонний сайт только по его
   выбору. */

export type WebchatState = '' | 'ok' | 'login' | 'challenge' | 'limit' | 'offline' | 'no_page' | 'not_sent' | 'no_answer' | 'failed';

export interface WebchatSiteStatus {
  id: WebchatId;
  state: WebchatState;
  stateAt: number;
  newChatsLeft?: number;
}

interface WebChatPlugin {
  ask(opts: { site: WebchatId; persona: string; prompt: string; fresh?: boolean; timeoutMs?: number }): Promise<{
    text: string;
    ms: number;
    site: WebchatId;
  }>;
  check(opts: { site: WebchatId }): Promise<{ state: WebchatState }>;
  open(opts: { site: WebchatId; title?: string; done?: string }): Promise<{ state: WebchatState | 'ready' | 'loading' }>;
  status(): Promise<{ sites: WebchatSiteStatus[]; profile: 'separate' | 'shared'; busy: string | null }>;
  forget(opts: { persona: string }): Promise<void>;
  reset(): Promise<{ profile: 'separate' | 'shared'; wiped: boolean }>;
}

export const WebChat = registerPlugin<WebChatPlugin>('WebChat');

const PREFS_KEY = 'vpc-phone-webchats';
const listeners = new Set<() => void>();

export function webchatPrefs(): WebchatPrefs {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null') as Partial<WebchatPrefs> | null;
    return { order: Array.isArray(raw?.order) ? raw.order : [], on: raw?.on && typeof raw.on === 'object' ? raw.on : {} };
  } catch {
    return { order: [], on: {} };
  }
}

export function setWebchatPrefs(next: WebchatPrefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(next));
  } catch {
    // нет хранилища — выбор живёт до перезапуска
  }
  for (const l of listeners) l();
}

export function onWebchatChange(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useWebchatPrefs(): WebchatPrefs {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => onWebchatChange(force), []);
  return webchatPrefs();
}

export class WebchatsFailed extends Error {
  code: string; // последний отказ: login, challenge, limit, offline, …
  constructor(code: string) {
    super(code);
    this.code = code;
  }
}

/**
 * Ответ первого откликнувшегося сайта. variants — промпты от полного к
 * короткому: сайту — первый, что влезает в его поле. fresh — вопрос не к
 * переписке (разбор напоминания): сайты со входом его не берут — не тратить
 * чат персоны. only — эти сайты в этом порядке (шаг порядка ответа); выключенный
 * сайт не спрашивается и тогда.
 */
export async function askWebchats(
  persona: string,
  variants: PromptMsg[][],
  fresh = false,
  only?: string[],
): Promise<{ text: string; site: WebchatSite }> {
  const prefs = webchatPrefs();
  const sites = only
    ? only.map((id) => WEBCHAT_SITES.find((s) => s.id === id)).filter((s): s is WebchatSite => !!s && prefs.on[s.id] === true)
    : enabledSites(prefs);
  let last = 'no_sites';
  for (const site of sites) {
    if (fresh && site.account) continue;
    const prompt = variants.reduce<string | null>((got, v) => got ?? fitPrompt(v, site.maxInput), null);
    if (!prompt) {
      last = 'too_long';
      continue;
    }
    try {
      const r = await WebChat.ask({ site: site.id, persona, prompt, fresh });
      if (r.text.trim()) {
        notifyStatus();
        return { text: r.text, site };
      }
      last = 'no_answer';
    } catch (e) {
      last = pluginCode(e) || 'failed';
      notifyStatus();
    }
  }
  throw new WebchatsFailed(last);
}

// Отказы, у которых есть свой текст (webchat.state.*); прочее — failed
const REASONS = ['ok', 'login', 'challenge', 'limit', 'offline', 'budget', 'no_page', 'not_sent', 'no_answer', 'too_long', 'busy'];

export function webchatReason(code: string): string {
  return REASONS.includes(code) ? code : 'failed';
}

/** Чаты персоны на сайтах со входом — забыть (переписка ушла на ядро или очищена). */
export function forgetWebchats(persona: string) {
  void WebChat.forget({ persona }).catch(() => {});
}

// Карточка «Провайдеры» перечитывает состояние сайтов после ответа
const statusListeners = new Set<() => void>();

function notifyStatus() {
  for (const l of statusListeners) l();
}

export function onWebchatStatus(cb: () => void): () => void {
  statusListeners.add(cb);
  return () => {
    statusListeners.delete(cb);
  };
}

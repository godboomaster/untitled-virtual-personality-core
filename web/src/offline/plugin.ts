import { registerPlugin } from '@capacitor/core';
import type { PluginListenerHandle } from '@capacitor/core';

/* Нативный плагин «OfflineLlm» (web/android/…/OfflineLlmPlugin.java): ответы
   персоны без ноутбука. Ключи провайдеров хранит и использует сам плагин —
   веб их не видит (только названия). Модель на телефоне — Gemma 4 E2B
   (LiteRT-LM), события загрузки и потоковой выдачи. */

export interface ChatMsg {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ModelStatus {
  downloaded: boolean;
  size: number;
  partial: number; // байт недокачанного файла
  downloading: boolean;
  bytes: number;
  total: number;
  loaded: boolean;
  backend: string | null; // gpu | cpu — на чём загружена
  unmetered: boolean; // сеть сейчас безлимитная (Wi-Fi)
  freeBytes: number;
}

export interface WebResult {
  title: string;
  body: string;
  href: string;
}

// Будильник напоминания (ReminderAlarms.java): ref — «id@время» ядра или «local:…»
export interface AlarmItem {
  ref: string;
  persona: string;
  name: string;
  text: string; // текст уведомления
  at: number; // unix-секунды
}

interface OfflineLlmPlugin {
  setProviders(opts: { persona: string; providers: unknown[] }): Promise<{ count: number }>;
  providersInfo(opts: { persona: string }): Promise<{ names: string[] }>;
  clearProviders(opts: { persona?: string }): Promise<void>;
  cloudChat(opts: { persona: string; messages: ChatMsg[]; maxTokens?: number; temperature?: number }): Promise<{
    text: string;
    provider: string;
    model: string;
  }>;
  modelStatus(): Promise<ModelStatus>;
  downloadModel(opts?: { url?: string; allowMetered?: boolean }): Promise<void>;
  cancelDownload(): Promise<void>;
  deleteModel(): Promise<void>;
  localChat(opts: {
    requestId: string;
    system: string;
    history: ChatMsg[];
    text: string;
    maxTokens?: number;
    temperature?: number;
  }): Promise<{ text: string; backend: string }>;
  stopLocal(): Promise<void>;
  unloadModel(): Promise<void>;
  webSearch(opts: { query: string; max?: number; engine?: 'ddg' | 'bing' }): Promise<{ results: WebResult[] }>;
  syncReminders(opts: { items: AlarmItem[]; probes: string[] }): Promise<{ armed: number; exact: boolean }>;
  reminderStatus(): Promise<{ pending: number; exact: boolean }>;
  markDelivered(opts: { refs: string[] }): Promise<void>;
  firedReminders(): Promise<{ items: (AlarmItem & { firedAt: number })[] }>;
  openExactAlarmSettings(): Promise<void>;
  addListener(event: 'modelProgress', cb: (d: { bytes: number; total: number }) => void): Promise<PluginListenerHandle>;
  addListener(event: 'modelDone', cb: () => void): Promise<PluginListenerHandle>;
  addListener(event: 'modelError', cb: (d: { message: string }) => void): Promise<PluginListenerHandle>;
  addListener(event: 'localToken', cb: (d: { requestId: string; text: string }) => void): Promise<PluginListenerHandle>;
}

export const OfflineLlm = registerPlugin<OfflineLlmPlugin>('OfflineLlm');

/** Код отказа плагина (call.reject(message, code)). */
export function pluginCode(e: unknown): string {
  return typeof e === 'object' && e !== null && 'code' in e ? String((e as { code: unknown }).code ?? '') : '';
}

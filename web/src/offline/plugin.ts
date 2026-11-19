import { registerPlugin } from '@capacitor/core';
import type { PluginListenerHandle } from '@capacitor/core';

/* Нативный плагин «OfflineLlm» (web/android/…/OfflineLlmPlugin.java): ответы
   персоны без ноутбука. Ключи провайдеров хранит и использует сам плагин —
   веб их не видит (только названия). Модель на телефоне — Gemma 4 E2B
   (LiteRT-LM), события загрузки и потоковой выдачи; модель векторов для
   поиска по смыслу плагин только скачивает. */

export interface ChatMsg {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface ModelStatus {
  downloaded: boolean; // считать есть чем: своя модель или соседнего приложения
  size: number;
  partial: number; // байт недокачанного файла
  downloading: boolean;
  bytes: number;
  total: number;
  loaded: boolean;
  backend: string | null; // gpu | cpu — на чём загружена
  unmetered: boolean; // сеть сейчас безлимитная (Wi-Fi)
  freeBytes: number;
  // Модель одна на два приложения (ядра и Арродеса, PhoneGemmaPeer.java):
  // own — своя, peer — модель соседа (его пакет в peer), он и считает
  via?: 'own' | 'peer' | null;
  peer?: string | null;
  own?: boolean; // своя модель скачана
  peerReason?: string | null; // почему не сосед: no_model | unreachable | not_installed | …
  busy?: boolean; // модель считает ответ (здесь или у соседа) — задачи ядра ждут
}

// Модель векторов (поиск по смыслу): файлы в dir, считает их WebView (embed.ts)
export interface EmbedStatus {
  downloaded: boolean;
  dir: string;
  size: number;
  partial: number; // байт на телефоне, считая недокачанное
  downloading: boolean;
  bytes: number;
  total: number;
  unmetered: boolean;
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

// Ключи, введённые на телефоне (без компьютера): вебу — только маски
export interface PhoneProvider {
  name: string;
  base_url: string;
  model: string;
  keys: string[]; // маски «sk-…abcd»
}

// Ключи с ядра у всех персон вместе (вкладка «Провайдеры»): маски и модели
export interface CoreProvider {
  name: string;
  models: string[];
  keys: string[]; // маски «sk-…abcd»
  personas: string[]; // у каких персон есть
}

// Кем персона может отвечать без ядра (её цепочка в досье, personaChain.ts):
// own — ключ с ядра пришёл ей самой; core — ключ с ядра (её или другой
// персоны: ключи ядра общие); phone — есть ключи, введённые на телефоне
export interface PersonaProvider {
  name: string;
  model: string;
  keys: string[]; // маски
  own: boolean;
  core: boolean;
  phone: boolean;
}

// Все ключи телефона по провайдерам (карточка «Ключи провайдеров», KeysCard):
// с ядра и введённые на телефоне; правки с телефона — поверх ключей ядра
export interface RingKey {
  id: string; // ключ ядра — отпечаток; ключ телефона — p<номер>
  mask: string;
  src: 'core' | 'phone';
  index?: number; // номер ключа телефона (updatePhoneProvider removeKey)
  hidden: boolean; // ключ ядра убран на телефоне
}

export interface RingProvider {
  name: string;
  base_url: string;
  model: string; // действующая на телефоне
  core_model: string;
  phone_model: string;
  edited: boolean; // своя модель или убранные ключи ядра
  personas: string[]; // у каких персон ключ с ядра
  keys: RingKey[];
}

// Целиком — только для переноса на компьютер (phoneKeys.ts)
export interface PhoneProviderSecret {
  name: string;
  base_url: string;
  model: string;
  api_keys: string[];
}

interface OfflineLlmPlugin {
  phoneProviders(): Promise<{ providers: PhoneProvider[] }>;
  updatePhoneProvider(opts: {
    name: string;
    base_url?: string;
    model?: string;
    temperature?: number;
    addKey?: string;
    removeKey?: number;
    remove?: boolean;
    top?: boolean;
  }): Promise<{ providers: PhoneProvider[] }>;
  exportPhoneProviders(): Promise<{ providers: PhoneProviderSecret[] }>;
  setProviders(opts: { persona: string; providers: unknown[] }): Promise<{ count: number }>;
  providersInfo(opts: { persona: string }): Promise<{ names: string[] }>;
  coreProviders(opts: { personas?: string[] }): Promise<{ providers: CoreProvider[] }>;
  personaProviders(opts: { persona: string }): Promise<{ providers: PersonaProvider[] }>;
  keyring(opts: { personas?: string[] }): Promise<{ providers: RingProvider[] }>;
  editProvider(opts: { name: string; model?: string; hideKey?: string; showKey?: string; reset?: boolean }): Promise<void>;
  clearProviders(opts: { persona?: string }): Promise<void>;
  // only — имена провайдеров в порядке перебора (порядок персоны, personaChain.ts); без него — все её
  cloudChat(opts: { persona: string; messages: ChatMsg[]; maxTokens?: number; temperature?: number; only?: string[] }): Promise<{
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
    topP?: number;
    deadline?: number; // Date.now(), после которого не начинать (задача ядра) — ошибка expired
  }): Promise<{ text: string; backend: string }>;
  stopLocal(): Promise<void>;
  embedStatus(): Promise<EmbedStatus>;
  downloadEmbed(opts?: { allowMetered?: boolean }): Promise<void>;
  cancelEmbed(): Promise<void>;
  deleteEmbed(): Promise<void>;
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
  addListener(event: 'embedProgress', cb: (d: { bytes: number; total: number }) => void): Promise<PluginListenerHandle>;
  addListener(event: 'embedDone', cb: () => void): Promise<PluginListenerHandle>;
  addListener(event: 'embedError', cb: (d: { message: string }) => void): Promise<PluginListenerHandle>;
  addListener(event: 'localToken', cb: (d: { requestId: string; text: string }) => void): Promise<PluginListenerHandle>;
}

export const OfflineLlm = registerPlugin<OfflineLlmPlugin>('OfflineLlm');

/** Код отказа плагина (call.reject(message, code)). */
export function pluginCode(e: unknown): string {
  return typeof e === 'object' && e !== null && 'code' in e ? String((e as { code: unknown }).code ?? '') : '';
}

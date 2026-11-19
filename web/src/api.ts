/* HTTP-клиент бэкенда (app/api/server.py). Базовый URL — из
   VITE_API_URL (см. web/.env), по умолчанию локальный сервер; адрес,
   введённый на экране запуска, хранится в localStorage (vpc-api-url) и
   важнее вшитого. В приложении на телефоне вшитого нет вовсе: ядро живёт на
   ноутбуке, адрес вводит пользователь.
   Токен (если на бэке задан API_TOKEN) вводится на экране запуска и
   хранится в localStorage под ключом vpc-api-token. */

import type { RoomArtData, RoomArtPatch, RoomFocus, RoomLayout, RoomLayoutPatch, RoomSource, RoomStyle, RoomView } from './room/roomTypes';
import type { EmotionMood, InitiativeBadge, PersonaMood, PortraitData, PortraitSheetUpdate } from './portrait/portraitTypes';
import type { AnimSettings, AnimUpdate, PortraitAnim, PortraitAnims } from './portrait/animTypes';
import type { FrameAtlas, FrameCut, FrameFix, FrameKind, FrameSourceData, FrameTiming } from './portrait/frames/frameTypes';
import { getLinkConfig, linkFetch, refusalKey } from './link/client.ts';
import type { PhoneActionReport } from './phone/notes';
import type { GateRules } from './offline/gate';
import type { LoreData } from './offline/lore';

const BUILD_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? 'http://127.0.0.1:8000';
const URL_KEY = 'vpc-api-url';

// Веб внутри приложения Android (Capacitor): мост ставит window.Capacitor
export function isNativeApp(): boolean {
  const cap = (window as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor;
  return Boolean(cap?.isNativePlatform?.());
}

function storedApiUrl(): string {
  try {
    return localStorage.getItem(URL_KEY) ?? '';
  } catch {
    return '';
  }
}

// Адрес ядра; '' — не задан (приложение на телефоне до первого подключения)
let baseUrl = storedApiUrl() || (isNativeApp() ? '' : BUILD_URL);

export function getApiUrl(): string {
  return baseUrl;
}

export function setApiUrl(url: string) {
  baseUrl = url || (isNativeApp() ? '' : BUILD_URL);
  try {
    if (url) localStorage.setItem(URL_KEY, url);
    else localStorage.removeItem(URL_KEY);
  } catch {
    /* хранилище недоступно — адрес проживёт до перезапуска */
  }
}

// Адрес бэкенда без схемы — для подписей в UI (экран загрузки);
// по защищённому каналу — имя ноутбука
export function apiHost(): string {
  const link = getLinkConfig();
  if (link) return `VPC Link · ${link.n}`;
  return baseUrl.replace(/^https?:\/\//, '');
}

/** Телефон сопряжён с ноутбуком: API — через защищённый канал (link/client.ts). */
export function isLinkMode(): boolean {
  return getLinkConfig() !== null;
}

// Ноутбук перестал узнавать телефон (отвязали на компьютере) — экран
// запуска (BootGate) ловит событие и объясняет, что делать
export const LINK_REVOKED_EVENT = 'vpc-link-revoked';

// Ядра нет (телефон без компьютера, адрес не задан): запрос ушёл бы на
// https://localhost самого WebView и вернул бы index.html — отказ сразу,
// как от недоступной сети
export class NoCoreError extends TypeError {
  constructor() {
    super('no core configured');
  }
}

// Все запросы к API — отсюда: по каналу VPC Link или обычным fetch
function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  if (!getLinkConfig() && !baseUrl) return Promise.reject(new NoCoreError());
  if (!getLinkConfig()) return fetch(`${baseUrl}${path}`, init);
  return linkFetch(path, init).catch((e: unknown) => {
    if (refusalKey(e) === 'link.errNotPaired') window.dispatchEvent(new Event(LINK_REVOKED_EVENT));
    throw e;
  });
}

/** Адрес из поля ввода → базовый URL ядра; null — не адрес.
 * Без схемы: IP, localhost или явный порт — http (ядро в локальной сети),
 * имя — https (обратный прокси обычно отдаёт ядро по HTTPS). */
export function normalizeApiUrl(input: string): string | null {
  let v = input.trim();
  if (!v) return null;
  if (!/^https?:\/\//i.test(v)) {
    const host = v.split('/')[0];
    const plain = /^(\d{1,3}\.){3}\d{1,3}(:\d+)?$/.test(host) || /^localhost(:\d+)?$/i.test(host) || /:\d+$/.test(host);
    v = `${plain ? 'http' : 'https'}://${v}`;
  }
  try {
    const u = new URL(v);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return `${u.origin}${u.pathname.replace(/\/+$/, '')}`;
  } catch {
    return null;
  }
}

const TOKEN_KEY = 'vpc-api-token';

export function getApiToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setApiToken(token: string) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* хранилище недоступно — токен проживёт до перезагрузки только в форме */
  }
}

// 401 от ядра: на бэке задан API_TOKEN, а токена нет или он неверный —
// экран запуска (BootGate) ловит событие и спрашивает токен
export const AUTH_REQUIRED_EVENT = 'vpc-auth-required';

function authHeader(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function noteAuthFailure(res: Response) {
  if (res.status === 401) window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
}

// Id этого устройства для очереди фоновых сообщений: у каждого клиента свой
// курсор, и сообщение получают и ноутбук, и телефон (см. app/api/inbox.py).
// Фоновая служба приложения на телефоне берёт его же с суффиксом "-bg"
// (bgInbox.ts) — свой курсор, сообщения веба не забирает
const CLIENT_KEY = 'vpc-client-id';
let clientId = '';

export function inboxClientId(): string {
  if (clientId) return clientId;
  try {
    clientId = localStorage.getItem(CLIENT_KEY) ?? '';
  } catch {
    /* хранилище недоступно — id проживёт до перезагрузки */
  }
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(clientId)) {
    clientId = Array.from({ length: 16 }, () => Math.floor(Math.random() * 36).toString(36)).join('');
    try {
      localStorage.setItem(CLIENT_KEY, clientId);
    } catch {
      /* см. выше */
    }
  }
  return clientId;
}

// Пользователь веб-интерфейса — один на всех персон (память у персон изолирована контекстом)
export const WEB_USER_ID = 'web_user';
// chat_id веб-чата: фичи ядра (todo/напоминания/обучение/проактивность) ключуются по chat_id,
// при chat_id=null они в process_message не срабатывают
export const WEB_CHAT_ID = 'web_user';

export interface DesktopPortraitStatus {
  connected: boolean;
  shown: boolean;
  persona: string;
  pending: { id: string; persona: string; shown: boolean } | null;
}

export interface ApiPersona {
  id: string;
  name: string;
  description: string;
  color?: string | null; // цвет метки персоны (общий календарь)
  features: Record<string, unknown>;
  settings: { temperature?: number; max_tokens?: number; top_p?: number };
  builtin?: boolean; // встроенная (в git/пакете аддона) — удалить нельзя
  customized?: boolean; // у встроенной есть своя копия — «удалить» сбрасывает к встроенной
}

// Местоположение пользователя (для окружения: локальное время и погода в контексте)
export interface LocationConfig {
  mode: 'off' | 'manual' | 'geo';
  city?: string;
  lat?: number;
  lon?: number;
}

// Часовой пояс пользователя (TIMEZONE, см. app/core/timeutil): timezone —
// сырое значение из .env ('' — не задано), effective — фактический пояс,
// source — 'env' (TIMEZONE распознан) | 'system' (не задан/не распознан)
export interface TimezoneConfig {
  timezone: string;
  effective: string;
  source: 'env' | 'system';
}

// «Экономия нейронки» (app/core/llm_economy): пункт → true — модель (как
// раньше), false — код (по умолчанию); groups — порядок пунктов в настройках
export interface LlmEconomy {
  items: Record<string, boolean>;
  defaults: Record<string, boolean>;
  groups: Record<string, string[]>;
}

// Скорость текста у аватара (app/core/avatar_text): слов в секунду (0 —
// сразу всей частью) и секунд на часть облачка средней длины
// Тема оформления веба на компьютере (app/core/ui_look): портрет на рабочем
// столе берёт её у ядра — браузерный выбор панели не виден
export interface UiLook {
  skin: string | null;
  personas: Record<string, string>;
  theme: 'light' | 'dark' | null;
}

export interface AvatarText {
  words_per_sec: number;
  part_sec: number;
  defaults: { words_per_sec: number; part_sec: number };
  limits: { words_per_sec: [number, number]; part_sec: [number, number] };
}

// Запись лога ядра (GET /api/logs — режим разработчика)
export interface LogEntry {
  seq: number;
  ts: number;
  level: string;
  logger: string;
  msg: string;
}

// Загрузка памяти хоста (GET /api/system/memory — виджет режима разработчика)
export interface SysMemory {
  ok: boolean;
  mem_percent?: number;
  mem_used_gb?: number;
  mem_total_gb?: number;
  mem_available_gb?: number;
  swap_used_gb?: number;
  swap_total_gb?: number;
}

export interface ApiHistoryMessage {
  role: string;
  content: string;
  timestamp: number | null;
  user_name: string | null;
  sender_id: string | null;
  files?: SentFile[] | null; // файлы, присланные этим ответом, пока ссылки живы
}

// Файл с компьютера ядра, присланный в чат («пришли файл X»): url отдаёт его
// под тем же токеном, пока ссылка жива (сутки, до перезапуска ядра)
export interface SentFile {
  id: string;
  name: string;
  size: number;
  url: string;
}

export interface ApiChatResponse {
  reply: string;
  extra_messages: string[];
  question_kind: string | null;
  persona: string;
  chat_id: string;
  provider: string | null; // кто реально ответил (с учётом fallback)
  model: string | null;
  control_mode?: boolean; // режим управления после этого сообщения — дебаунс отправки гасится
  phone_mode?: boolean; // режим управления телефоном (app/features/phone_control)
  images?: string[]; // скриншоты страницы (dataURL) из режима управления
  reply_ts?: number | null; // метка ответа в STM (серверные секунды) — место пузыря в ленте
  answer_options?: AnswerOptions | null; // кнопки ответа на вопрос режима управления
  task?: TaskCard | null; // карточка задачи агента (итог хода; дальше — inbox)
  files?: SentFile[]; // файлы с компьютера ядра («пришли файл X»)
  reply_stm?: string | null; // текст ответа в истории, если другой (список файлов — заглушкой)
  cancelled?: boolean; // ход остановлен («Стоп»): ответа нет, реплика в истории
}

// Карточка задачи агента (TaskAgent.task_card): статус, план заказа с
// отметками и журнал хода — те же строки, что ушли в чат (фронт прячет
// их из пузырей, пока карточка видна)
export type TaskStatus = 'working' | 'ask' | 'confirm' | 'paused' | 'done' | 'payment' | 'cancelled' | 'stopped';
export interface TaskCard {
  id: string;
  goal: string;
  status: TaskStatus;
  steps: number;
  calls?: number; // вызовов модели за задачу; старое ядро поле не шлёт
  started: number; // unix-секунды сервера — место карточки в ленте
  updated: number;
  plan: { text: string; state: 'done' | 'current' | 'todo' }[];
  log: { text: string; ok: boolean }[];
}

// Кнопки ответа на вопрос режима управления (BotInstance.cc_answer_options):
// вариант с подписью шлёт send (номер), вариант с role — «да»/«нет»/«отмена»
// на языке интерфейса. Выбор уходит обычной репликой — бэкенд разбирает её
// как набранную, с теми же проверками подтверждений
export interface AnswerOption {
  label?: string;
  send?: string;
  role?: 'yes' | 'no' | 'cancel';
}
export interface AnswerOptions {
  kind: 'options' | 'yesno' | 'continue';
  options: AnswerOption[];
  // Кнопки и вне режима управления компьютером: список «какой файл?»,
  // вопросы экрана телефона («Где управлять?», «Нажать «X»?», «Какое?»)
  scope?: 'files' | 'phone';
}

// Части «Очистить диалог» (app/api/schemas.py: ClearPart) — в порядке кнопок досье
export const CLEAR_PARTS = [
  'stm', 'ltm', 'diary', 'todo', 'reminders', 'dossier',
  'learning', 'initiatives', 'rhythm', 'living', 'webchat', 'control',
] as const;
export type ClearPart = (typeof CLEAR_PARTS)[number];

export class ApiError extends Error {
  status: number;
  data?: Record<string, unknown>; // тело ответа с ошибкой (доп. флаги, например memory_exists)
  constructor(status: number, detail: string, data?: Record<string, unknown>) {
    super(detail);
    this.status = status;
    this.data = data;
  }
}

// Маршрута нет в ядре вообще (а не «нет такой записи»): ядро запущено с кодом
// старше веба — 404 FastAPI без своего текста
export function isMissingRoute(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404 && e.message === 'Not Found';
}

// Под id осталась память удалённой персоны (409 при создании/смене id):
// keep — подхватить, fresh — старую в архив
export type MemoryChoice = 'keep' | 'fresh';

export function isMemoryConflict(e: unknown): e is ApiError {
  return e instanceof ApiError && e.status === 409 && e.data?.memory_exists === true;
}

/** Стрим ответа оборвался без финального события (перезагрузка/сон ноутбука,
 * обрыв сети, рестарт прокси): это НЕ ошибка генерации — сервер, скорее
 * всего, дописал (или допишет) ответ в STM, чат догружает его из истории.
 * Ошибки сервера (event.error, HTTP-статус) остаются обычным ApiError. */
export class StreamInterruptedError extends ApiError {
  constructor(detail: string) {
    super(0, detail);
  }
}

/** Аватар: id персоны (обычный) или «персона@режим» — аватар её режима
 * (свои листы и анимации у YAML режима; app/api/avatar_modes.py). */
export function splitAvatar(avatar: string): { persona: string; mode: string } {
  const i = avatar.indexOf('@');
  return i < 0 ? { persona: avatar, mode: '' } : { persona: avatar.slice(0, i), mode: avatar.slice(i + 1) };
}

export const avatarOf = (persona: string, mode?: string | null) => (mode ? `${persona}@${mode}` : persona);

/** Путь портрета аватара: /api/personas/<p>/portrait<rest>, режим — ?mode= */
function portraitUrl(avatar: string, rest = '', params: Record<string, string> = {}): string {
  const { persona, mode } = splitAvatar(avatar);
  const q = new URLSearchParams(params);
  if (mode) q.set('mode', mode);
  const qs = q.toString();
  return `/api/personas/${encodeURIComponent(persona)}/portrait${rest}${qs ? `?${qs}` : ''}`;
}

const framesPath = (avatar: string, kind: FrameKind, id: string, tail = '') =>
  portraitUrl(avatar, `/frames/${kind}/${encodeURIComponent(id)}${tail}`);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...authHeader(),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    noteAuthFailure(res);
    let detail = `HTTP ${res.status}`;
    let data: Record<string, unknown> | undefined;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
      if (body && typeof body === 'object') data = body as Record<string, unknown>;
    } catch {
      /* тело не JSON — оставляем HTTP-код */
    }
    throw new ApiError(res.status, detail, data);
  }
  return res.json() as Promise<T>;
}

// VPC Link: состояние канала до телефона (GET /api/link)
export interface LinkDevice {
  id: string;
  name: string;
  paired_at: number;
  last_seen: number;
  online: 'lan' | 'relay' | null; // через что подключён сейчас
}

export interface LinkStatus {
  enabled: boolean;
  running: boolean;
  port: number;
  lan: string[];
  lan_error: string | null;
  relay: string;
  relay_connected: boolean;
  relay_error: string | null;
  devices: LinkDevice[];
  pairing_expires: number | null;
  this_device: string | null; // запрос пришёл с этого телефона
}

// Персона-кит для ответов без ноутбука (app/api/offline_api.py: build_kit)
export interface OfflineHistoryLine {
  role: 'user' | 'assistant';
  text: string;
  ts: number;
  vec?: string; // вектор текста (int8, base64) — в памяти для телефона
}

// Память персоны для телефона: вся переписка чата и факты (кит версии 3);
// с векторами из базы ядра — для поиска по смыслу (vectors — их модель)
export interface OfflineMemoryResponse {
  persona: string;
  generated_at: number;
  history: OfflineHistoryLine[];
  facts: string[];
  vectors?: { model: string; dim: number } | null;
  facts_vec?: string[]; // по порядку facts, '' — вектора нет
}

// Краткое описание персоны для модели телефона (app/features/short_prompt.py):
// source — yaml (своё: поле short_prompt персоны) или auto (составила модель);
// status — ready, pending (составляется), failed (error — почему), none;
// composing — готовое есть, а модель составляет новое (не вышло — error при
// ready); prompt — запрос карточки одним текстом для своей нейросети
export interface ShortPrompt {
  text: string;
  source: 'yaml' | 'auto' | '';
  status: 'ready' | 'pending' | 'failed' | 'none';
  at: number;
  chars?: number; // символов в полном характере
  error?: string;
  composing?: boolean;
  prompt?: string;
}

export interface OfflineKitResponse {
  version: number;
  persona: string;
  name: string;
  names?: string[]; // обращения к персоне (trigger_words, id, имя) — срезать из напоминания
  generated_at: number;
  system: string; // целиком — для облачной модели
  // short — краткое описание для модели телефона (пусто — ещё не составлено),
  // rules_local — правила офлайна короче; у ядра до них — нет
  parts: { character: string; rules: string; style: string; language: string; short?: string; rules_local?: string };
  short_prompt?: Pick<ShortPrompt, 'source' | 'status' | 'at'>;
  lang?: string | null; // язык чата — запасной (язык ответа — по текущей реплике)
  facts: string[];
  history: OfflineHistoryLine[];
  // Версия 3: сообщения старше на ноутбуке стёрты (0 — переписки нет); строка
  // досье и личная память персоны — как в ответе ядра
  history_first_ts?: number;
  history_total?: number; // сообщений в памяти для телефона (GET …/offline/memory)
  lore_stamp?: string; // версия лора персоны (GET …/offline/lore); '' — лора нет
  dossier?: string;
  self_memory?: string;
  // Город для погоды (её телефон берёт у Open-Meteo сам); null — выключено
  env: { city: string; lat: number; lon: number; codes: Record<string, string> } | null;
  // Срабатывания напоминаний чата на две недели — будильником телефона
  reminders: { id: string; ref: string; task: string | null; at: number; recurring: boolean }[];
  reminders_enabled: boolean;
  // те же правила, что в промпте ядра (self_memory, recall* — с версии 3)
  rules: {
    web: string;
    env: string;
    self_memory?: string;
    recall_head?: string;
    recall?: string;
    lore_head?: string;
    lore_weak?: string;
    lore?: string;
    lore_gap?: string;
  };
  notes: Record<'set' | 'ask_time' | 'past' | 'recurring' | 'scheduled' | 'extract', string> &
    Partial<Record<'not_understood' | 'declined', string>>;
  // Гейт веб-поиска (app/features/search_gate.py): чёрный список, белый (need) и пункт
  // «Экономии нейронки» every_message — порт в offline/gate.ts
  search_gate: GateRules;
  // Режимы персоны (app/core/persona.py mode_kit): режим чата сейчас ("" —
  // обычный), журнал [время, режим], пометка ответа каждого режима, строка
  // «режим сейчас», правило к пометкам ({tags}) и заметка без пометок;
  // null — режимов нет (offline/modeTags.ts)
  // turn — заметка к реплике, если последний ответ в истории другого режима
  // ({like} — образец голоса на языке реплики)
  modes?: { current: string; log: [number, string][]; tags: Record<string, string>; now: string; tagged: string; note: string; turn?: string } | null;
  // Образцы голоса нынешнего режима (voice_examples) и заметка к ним ({lines})
  voice?: { lines: string[]; note: string } | null;
}

export const api = {
  health: (init?: RequestInit) => request<{ status: string; id?: string }>('/api/health', init),

  // VPC Link: телефоны, QR-код сопряжения, отвязка
  getLink: () => request<LinkStatus>('/api/link'),
  pairLink: () => request<{ uri: string; expires: number; ttl: number }>('/api/link/pair', { method: 'POST' }),
  cancelPairLink: () => request<{ ok: boolean }>('/api/link/pair', { method: 'DELETE' }),
  unpairLink: (id: string) =>
    request<{ ok: boolean }>(`/api/link/devices/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // API_TOKEN ядра для входа по адресу и токену; null — в .env не задан
  getServerToken: () => request<{ token: string | null }>('/api/token'),

  // Ответы без ноутбука (приложение на телефоне, web/src/offline): персона-кит,
  // ключи API-провайдеров персоны, приём офлайн-переписки
  getOfflineKit: (persona: string) =>
    request<OfflineKitResponse>(
      `/api/personas/${encodeURIComponent(persona)}/offline/kit?chat_id=${encodeURIComponent(WEB_CHAT_ID)}&user_id=${encodeURIComponent(WEB_USER_ID)}`,
    ),
  getShortPrompt: (persona: string) =>
    request<ShortPrompt>(`/api/personas/${encodeURIComponent(persona)}/offline/short-prompt`),
  // Своё описание (пустое — убрать, составит модель персоны) или составленное
  // на телефоне без ядра (source auto, crc — crc32 характера; другой — 409)
  setShortPrompt: (persona: string, text: string, source: 'yaml' | 'auto' = 'yaml', crc?: number) =>
    request<ShortPrompt>(`/api/personas/${encodeURIComponent(persona)}/offline/short-prompt`, {
      method: 'PUT',
      body: JSON.stringify({ text, source, ...(crc != null ? { crc } : {}) }),
    }),
  // «Составить заново» моделью персоны (своё описание убирается)
  composeShortPrompt: (persona: string) =>
    request<ShortPrompt>(`/api/personas/${encodeURIComponent(persona)}/offline/short-prompt/compose`, { method: 'POST' }),
  getOfflineMemory: (persona: string) =>
    request<OfflineMemoryResponse>(
      `/api/personas/${encodeURIComponent(persona)}/offline/memory?chat_id=${encodeURIComponent(WEB_CHAT_ID)}&user_id=${encodeURIComponent(WEB_USER_ID)}`,
    ),
  getOfflineLore: (persona: string) =>
    request<LoreData>(`/api/personas/${encodeURIComponent(persona)}/offline/lore`),
  getOfflineProviders: (persona: string) =>
    request<{ providers: unknown[] }>(`/api/personas/${encodeURIComponent(persona)}/offline/providers`),
  offlineReminders: (persona: string, reminders: { id: string; task: string; at: number }[]) =>
    request<{ created: Record<string, string>; past: string[]; enabled: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/offline/reminders`,
      { method: 'POST', body: JSON.stringify({ user_id: WEB_USER_ID, chat_id: WEB_CHAT_ID, reminders }) },
    ),
  offlineImport: (persona: string, messages: { id: string; role: 'user' | 'assistant'; text: string; ts: number; source?: string; mode?: string }[]) =>
    request<{ imported: number; skipped: number }>(`/api/personas/${encodeURIComponent(persona)}/offline/import`, {
      method: 'POST',
      body: JSON.stringify({ user_id: WEB_USER_ID, chat_id: WEB_CHAT_ID, messages }),
    }),

  getPersonas: () => request<ApiPersona[]>('/api/personas'),

  // Создать персону из YAML (имя файла = поле id; 409 — такая уже есть)
  // memory — выбор при оставшейся под id памяти (без него — 409, isMemoryConflict)
  createPersona: (yaml: string, memory?: MemoryChoice) =>
    request<{ ok: boolean; persona: string }>('/api/personas', {
      method: 'POST',
      body: JSON.stringify({ yaml, memory }),
    }),

  // reset — удалена своя копия встроенной персоны, осталась встроенная
  deletePersona: (persona: string) =>
    request<{ status: string; reset?: boolean }>(`/api/personas/${encodeURIComponent(persona)}`, { method: 'DELETE' }),

  // Копия персоны: новый id ({id}_copy) и имя + «(копия)»
  duplicatePersona: (persona: string) =>
    request<{ ok: boolean; persona: string }>(
      `/api/personas/${encodeURIComponent(persona)}/duplicate`,
      { method: 'POST' },
    ),

  // Пауза голосовой активации ядра («Коннор, …», WAKE_LISTEN) на seconds,
  // 0 — снять: пока открыт голосовой режим чата, голосом занят он
  getDesktopPortrait: () => request<DesktopPortraitStatus>('/api/desktop/portrait'),
  setDesktopPortrait: (persona: string, shown: boolean) =>
    request<DesktopPortraitStatus>('/api/desktop/portrait', {
      method: 'POST',
      body: JSON.stringify({ persona, shown }),
    }),

  holdWake: (seconds: number) =>
    request<{ held: boolean }>('/api/voice/wake/hold', {
      method: 'POST',
      body: JSON.stringify({ seconds }),
    }),

  sendChat: (params: {
    persona: string;
    message: string;
    userId?: string;
    userName?: string;
    replyContext?: string;
  }) =>
    request<ApiChatResponse>('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        persona: params.persona,
        message: params.message,
        user_id: params.userId ?? WEB_USER_ID,
        chat_id: WEB_CHAT_ID,
        user_name: params.userName ?? null,
        reply_context: params.replyContext ?? null,
        // Пока ответа нет, реплику показывают другие устройства, а не это
        client_id: inboxClientId(),
      }),
    }),

  getHistory: (persona: string, userId: string = WEB_USER_ID) =>
    request<ApiHistoryMessage[]>(
      `/api/chat/history?persona=${encodeURIComponent(persona)}&user_id=${encodeURIComponent(userId)}`,
    ),

  // Очистка диалога: без parts — всё сразу (переписка, факты, дневник,
  // инициативы, веб-чаты, дела, напоминания, досье, обучение, ритм, живое
  // состояние, режим управления); parts — только эти части. Перед сбросом
  // бэкенд пишет снапшот в корзину (7 дней) — см. restoreClearBackup
  clearChat: (persona: string, parts?: ClearPart[], userId: string = WEB_USER_ID) =>
    request<{ status: string }>('/api/chat/clear', {
      method: 'POST',
      body: JSON.stringify({ persona, user_id: userId, chat_id: WEB_CHAT_ID, ...(parts ? { parts } : {}) }),
    }),

  // «Стоп»: оборвать ответ, который персона сейчас пишет в этот чат (ответ в
  // историю не ляжет, стрим хода закончится cancelled). cancelled — сколько
  // ходов оборвано; stopping — работает агент: он доделает шаг и ответит
  // итогом; 0 и без stopping — останавливать нечего (ответ уже готов)
  cancelChat: (persona: string, userId: string = WEB_USER_ID) =>
    request<{ cancelled: number; stopping?: boolean }>('/api/chat/cancel', {
      method: 'POST',
      body: JSON.stringify({ persona, user_id: userId, chat_id: WEB_CHAT_ID }),
    }),

  // Корзина очистки: снапшот последнего сброса (parts — стёртые части, null — всё)
  getClearBackup: (persona: string) =>
    request<{
      exists: boolean;
      ts?: number;
      counts?: { stm: number; ltm: number; diary: boolean; initiatives?: number };
      parts?: ClearPart[] | null;
    }>(`/api/personas/${encodeURIComponent(persona)}/clear-backup`),

  restoreClearBackup: (persona: string) =>
    request<{ status: string; restored: { stm: number; ltm: number; diary: boolean } }>(
      `/api/personas/${encodeURIComponent(persona)}/clear-backup/restore`,
      { method: 'POST', body: JSON.stringify({}) },
    ),

  // Поштучное удаление из STM: index — позиция в списке getHistory.
  // match — текст (и метка) удаляемой реплики: буфер STM сдвигается с каждой
  // новой репликой, сервер тогда ищет именно её, а index — лишь подсказка
  deleteStmMessage: (persona: string, index: number, match?: { content: string; timestamp?: number | null }) =>
    request<{ status: string }>('/api/chat/history/delete', {
      method: 'POST',
      body: JSON.stringify({
        persona, index, user_id: WEB_USER_ID, chat_id: WEB_CHAT_ID,
        ...(match ? { content: match.content, timestamp: match.timestamp ?? null } : {}),
      }),
    }),

  // Удаление последних count сообщений из STM
  trimStm: (persona: string, count: number) =>
    request<{ status: string; deleted: number }>('/api/chat/history/trim', {
      method: 'POST',
      body: JSON.stringify({ persona, count, user_id: WEB_USER_ID, chat_id: WEB_CHAT_ID }),
    }),

  getLtmFacts: (persona: string, userId: string = WEB_USER_ID) =>
    request<string[]>(
      `/api/personas/${encodeURIComponent(persona)}/memory/ltm?user_id=${encodeURIComponent(userId)}`,
    ),

  // Профиль досье чата: интересы/темы/наблюдения из автоанализа диалога (не LTM)
  getDossier: (persona: string) =>
    request<{ interests: string[]; topics: string[]; personality_notes: string[] }>(
      `/api/personas/${encodeURIComponent(persona)}/dossier?chat_id=${WEB_CHAT_ID}`,
    ),

  addFact: (persona: string, fact: string, userId: string = WEB_USER_ID) =>
    request<{ status: string }>(`/api/personas/${encodeURIComponent(persona)}/memory/facts`, {
      method: 'POST',
      body: JSON.stringify({ fact, user_id: userId }),
    }),

  // Замена факта отредактированным текстом (old — исходная строка «Категория: факт»)
  updateFact: (persona: string, oldText: string, newText: string, userId: string = WEB_USER_ID) =>
    request<{ status: string; old: string }>(`/api/personas/${encodeURIComponent(persona)}/memory/facts`, {
      method: 'PUT',
      body: JSON.stringify({ old: oldText, new: newText, user_id: userId }),
    }),

  forgetFact: (persona: string, query: string, userId: string = WEB_USER_ID) =>
    request<{ status: string; removed: string }>(
      `/api/personas/${encodeURIComponent(persona)}/memory/facts?query=${encodeURIComponent(query)}&user_id=${encodeURIComponent(userId)}`,
      { method: 'DELETE' },
    ),

  // ── Дела / напоминания / инвентарь / обучение / дневник / инициатива ──

  getTodo: (persona: string) =>
    request<{ items: TodoEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/todo?chat_id=${WEB_CHAT_ID}`),

  addTodo: (persona: string, task: string) =>
    request<{ items: TodoEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/todo`, {
      method: 'POST',
      body: JSON.stringify({ task, chat_id: WEB_CHAT_ID }),
    }),

  removeTodo: (persona: string, index: number) =>
    request<{ items: TodoEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/todo?index=${index}&chat_id=${WEB_CHAT_ID}`,
      { method: 'DELETE' },
    ),

  // Заметки пользователя (блок на главной): книжка персоны, свежие сверху
  getNotes: (persona: string) =>
    request<{ items: NoteEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/notes`),
  // Заметки, записанные на телефоне без ядра (offline/localNotes.ts)
  importNotes: (persona: string, notes: { id: string; text: string; created_at: number; updated_at: number | null }[]) =>
    request<{ imported: number; skipped: number }>(`/api/personas/${encodeURIComponent(persona)}/notes/import`, {
      method: 'POST',
      body: JSON.stringify({ notes }),
    }),

  addNote: (persona: string, text: string) =>
    request<{ note: NoteEntry }>(`/api/personas/${encodeURIComponent(persona)}/notes`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),

  updateNote: (persona: string, id: string, text: string) =>
    request<{ note: NoteEntry }>(
      `/api/personas/${encodeURIComponent(persona)}/notes/${encodeURIComponent(id)}`,
      { method: 'PATCH', body: JSON.stringify({ text }) },
    ),

  deleteNote: (persona: string, id: string) =>
    request<{ ok: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/notes/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),

  // ── Сервисы (app/integrations): почта, Диск, GitHub, Telegram, ленты, вебхук ──
  getServices: () => request<ServicesStatus>('/api/integrations'),

  putServicesSettings: (patch: { notify?: Partial<ServicesNotify>; services?: Record<string, Record<string, unknown>> }) =>
    request<{ ok: boolean; settings: ServicesSettings }>('/api/integrations/settings', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  addServiceRule: (rule: { action: ServiceRule['action']; source: string; match: string }) =>
    request<{ ok: boolean; rule: ServiceRule }>('/api/integrations/rules', {
      method: 'POST',
      body: JSON.stringify(rule),
    }),

  deleteServiceRule: (id: string) =>
    request<{ ok: boolean }>(`/api/integrations/rules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  clearServiceExamples: () => request<{ ok: boolean }>('/api/integrations/examples/clear', { method: 'POST' }),

  setServicesDnd: (minutes: number) =>
    request<{ ok: boolean; dnd_until: number }>('/api/integrations/dnd', {
      method: 'POST',
      body: JSON.stringify({ minutes }),
    }),

  getServicesFeed: (limit = 80, source?: string) =>
    request<{ items: ServiceFeedItem[] }>(
      `/api/integrations/feed?limit=${limit}${source ? `&source=${encodeURIComponent(source)}` : ''}`,
    ),

  rateServiceItem: (id: string, level: ServiceLevel) =>
    request<{ ok: boolean; rule: ServiceRule | null }>(
      `/api/integrations/feed/${encodeURIComponent(id)}/feedback`,
      { method: 'POST', body: JSON.stringify({ level }) },
    ),

  clearServicesFeed: () => request<{ ok: boolean }>('/api/integrations/feed/clear', { method: 'POST' }),

  testServicesNotify: () =>
    request<{ ok: boolean; detail?: string }>('/api/integrations/test-notify', { method: 'POST' }),

  servicesDigestNow: () =>
    request<{ ok: boolean; sent: number }>('/api/integrations/digest-now', { method: 'POST' }),

  checkService: (service: string) =>
    request<ServiceCheck>(`/api/integrations/${encodeURIComponent(service)}/check`, { method: 'POST' }),

  pollService: (service: string) =>
    request<{ ok: boolean }>(`/api/integrations/${encodeURIComponent(service)}/poll`, { method: 'POST' }),

  disconnectService: (account: string) =>
    request<{ ok: boolean }>(`/api/integrations/${encodeURIComponent(account)}`, { method: 'DELETE' }),

  setGoogleClient: (body: { client_id?: string; client_secret?: string; json_text?: string }) =>
    request<{ ok: boolean }>('/api/integrations/google/client', { method: 'POST', body: JSON.stringify(body) }),

  startGoogleLogin: () => request<{ url: string }>('/api/integrations/google/login', { method: 'POST' }),

  googleLoginResult: () =>
    request<{ result: { ok: boolean; error?: string; email?: string } | null; connected: boolean; email: string }>(
      '/api/integrations/google/login',
    ),

  finishGoogleLogin: (url: string) =>
    request<{ ok: boolean; error?: string; email?: string }>('/api/integrations/google/finish', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),

  setGithubToken: (token: string) =>
    request<{ ok: boolean; login?: string; error?: string }>('/api/integrations/github/token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    }),

  telegramLogin: (body: { api_id: number; api_hash: string; phone: string }) =>
    request<ServiceLoginStep>('/api/integrations/telegram/login', { method: 'POST', body: JSON.stringify(body) }),

  telegramCode: (code: string) =>
    request<ServiceLoginStep>('/api/integrations/telegram/code', { method: 'POST', body: JSON.stringify({ code }) }),

  telegramPassword: (password: string) =>
    request<ServiceLoginStep>('/api/integrations/telegram/password', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),

  connectImap: (body: { user: string; password: string; host?: string; port?: number; folder?: string }) =>
    request<{ ok: boolean; host?: string; error?: string }>('/api/integrations/imap', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  imapPreset: (user: string) =>
    request<{ host: string; port: number }>(`/api/integrations/imap/preset?user=${encodeURIComponent(user)}`),

  checkRssFeed: (url: string) =>
    request<ServiceCheck>('/api/integrations/rss/check', { method: 'POST', body: JSON.stringify({ url }) }),

  regenerateWebhook: () =>
    request<{ ok: boolean; path: string }>('/api/integrations/webhook/regenerate', { method: 'POST' }),

  // Сервисы телефона без компьютера → ядро (offline/phoneServices.ts): ключи,
  // курсоры, правила и лента; ядро — главное, свои аккаунты не заменяет
  importPhoneServices: (bundle: object) =>
    request<{
      ok: boolean;
      moved: string[];
      skipped: Record<string, string>;
      rules: number;
      items: number;
      notify: string[];
    }>('/api/integrations/import', { method: 'POST', body: JSON.stringify(bundle) }),

  getReminders: (persona: string) =>
    request<{ items: ReminderEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/reminders?chat_id=${WEB_CHAT_ID}`,
    ),

  // ── Общий календарь всех персон ──

  getCalendar: (start: string, end: string) =>
    request<{ items: CalendarEntry[] }>(
      `/api/calendar?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    ),

  addCalendarEntry: (entry: {
    title: string;
    date: string; // YYYY-MM-DD
    time?: string | null; // HH:MM
    kind?: CalendarKind;
    persona?: string | null;
    note?: string;
  }) =>
    request<CalendarEntry>('/api/calendar', {
      method: 'POST',
      body: JSON.stringify({ user_name: 'web', ...entry }),
    }),

  updateCalendarEntry: (id: string, patch: Partial<{
    title: string;
    date: string;
    time: string | null;
    kind: CalendarKind;
    persona: string | null;
    note: string;
    done: boolean;
  }>) =>
    request<CalendarEntry>(`/api/calendar/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  deleteCalendarEntry: (id: string) =>
    request<{ status: string }>(`/api/calendar/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // recurrence — повтор; первое срабатывание — ближайшее по расписанию не раньше delay
  addReminder: (persona: string, task: string, delaySeconds: number, recurrence?: ReminderRecurrenceInput | null) =>
    request<{ items: ReminderEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/reminders`, {
      method: 'POST',
      body: JSON.stringify({
        task, delay_seconds: delaySeconds, chat_id: WEB_CHAT_ID,
        ...(recurrence ? { recurrence } : {}),
      }),
    }),

  cancelReminder: (persona: string, index: number) =>
    request<{ items: ReminderEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/reminders?index=${index}&chat_id=${WEB_CHAT_ID}`,
      { method: 'DELETE' },
    ),

  // Отмена по стабильному id: номер строки мог сдвинуться (одно сработало)
  cancelReminderById: (persona: string, id: string) =>
    request<{ items: ReminderEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/reminders?id=${encodeURIComponent(id)}&chat_id=${WEB_CHAT_ID}`,
      { method: 'DELETE' },
    ),

  // Правка на месте по id: текст, время (unix-секунды), пауза (active: false —
  // на паузу, true — продолжить) и/или повтор (не передан — прежний, null — снять)
  updateReminder: (persona: string, id: string, patch: {
    task?: string; trigger_at?: number; active?: boolean; recurrence?: ReminderRecurrenceInput | null;
  }) =>
    request<{ items: ReminderEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/reminders/${encodeURIComponent(id)}`,
      { method: 'PUT', body: JSON.stringify({ ...patch, chat_id: WEB_CHAT_ID }) },
    ),

  getInventory: (persona: string) =>
    request<{ items: InventoryEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/inventory`),

  addInventoryItem: (persona: string, name: string, description: string = '') =>
    request<{ result: string }>(`/api/personas/${encodeURIComponent(persona)}/inventory`, {
      method: 'POST',
      body: JSON.stringify({ name, description }),
    }),

  removeInventoryItem: (persona: string, name: string) =>
    request<{ result: string }>(
      `/api/personas/${encodeURIComponent(persona)}/inventory?name=${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    ),

  // ── Комната: живое присутствие персоны (типы — web/src/room/roomTypes.ts) ──
  // chat_id=auto — бэкенд сам берёт самый свежий чат (веб или Telegram)
  getRoom: (persona: string, chatId: string = 'auto') =>
    request<RoomView>(`/api/personas/${encodeURIComponent(persona)}/room?chat_id=${encodeURIComponent(chatId)}`),

  getRoomLayout: (persona: string) =>
    request<RoomLayout>(`/api/personas/${encodeURIComponent(persona)}/room/layout`),

  // Частичный патч раскладки; null у предмета удаляет его запись
  putRoomLayout: (persona: string, patch: RoomLayoutPatch) =>
    request<RoomLayout>(`/api/personas/${encodeURIComponent(persona)}/room/layout`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  getRoomStyle: (persona: string) =>
    request<RoomStyle>(`/api/personas/${encodeURIComponent(persona)}/room/style`),

  putRoomStyle: (persona: string, patch: { description?: string; reference?: string | null }) =>
    request<RoomStyle>(`/api/personas/${encodeURIComponent(persona)}/room/style`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  // Описание арт-стиля по референсу (vision-модель; 501 — такой модели нет)
  describeRoomStyle: (persona: string, reference: string) =>
    request<{ description: string }>(`/api/personas/${encodeURIComponent(persona)}/room/style/describe`, {
      method: 'POST',
      body: JSON.stringify({ reference }),
    }),

  getRoomArt: (persona: string) =>
    request<RoomArtData>(`/api/personas/${encodeURIComponent(persona)}/room/art`),

  putRoomArt: (persona: string, patch: RoomArtPatch) =>
    request<RoomArtData>(`/api/personas/${encodeURIComponent(persona)}/room/art`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  // Живой портрет (голосовой режим): листы ракурсов головы. avatar — id
  // персоны или «персона@режим» (splitAvatar)
  getPortrait: (avatar: string) => request<PortraitData>(portraitUrl(avatar)),
  getInitiativeBadge: (persona: string) =>
    request<InitiativeBadge>(portraitUrl(persona, '/initiative-badge')),
  putInitiativeBadge: (persona: string, badge: InitiativeBadge) =>
    request<InitiativeBadge>(portraitUrl(persona, '/initiative-badge'), {
      method: 'PUT', body: JSON.stringify(badge),
    }),

  // Что показывать сейчас: аватар режима, включённого в основном чате
  // (mode — режим, avatar — чьи листы; "" — обычные)
  getLivePortrait: (persona: string) => request<PortraitData>(portraitUrl(persona, '', { live: '1' })),

  // Что есть у аватаров режимов персоны ("" — обычный) и режим чата сейчас
  getPortraitModes: (persona: string) => request<PortraitModesOverview>(portraitUrl(persona, '/modes')),

  // Анимации перехода из режима from в режим to ("" — обычный) с атласами
  getPortraitSwitch: (persona: string, from: string, to: string) =>
    request<{ anims: PortraitAnim[] }>(portraitUrl(persona, '/switch', { from, to })),

  // sheet — ключ листа: «A», «VO» или лист эмоции «id.A» (sheetKey)
  putPortraitSheet: (avatar: string, sheet: string, body: PortraitSheetUpdate) =>
    request<PortraitData>(portraitUrl(avatar, `/${encodeURIComponent(sheet)}`), {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  deletePortraitSheet: (avatar: string, sheet: string) =>
    request<PortraitData>(portraitUrl(avatar, `/${encodeURIComponent(sheet)}`), {
      method: 'DELETE',
    }),

  // Эмоции портрета: id 'new' — новая (её id — в created ответа); mood:
  // null — только по маркеру, не передан — прежнее условие
  putPortraitEmotion: (avatar: string, id: string, name: string, hint: string, mood?: EmotionMood | null) =>
    request<PortraitData>(portraitUrl(avatar, `/emotions/${encodeURIComponent(id)}`), {
      method: 'PUT',
      body: JSON.stringify(mood === undefined ? { name, hint } : { name, hint, mood }),
    }),

  // Настроение персоны сейчас (null — живого состояния нет): эмоции «по настроению»
  getPortraitMood: (persona: string) =>
    request<{ mood: PersonaMood | null }>(portraitUrl(splitAvatar(persona).persona, '/mood')),

  deletePortraitEmotion: (avatar: string, id: string) =>
    request<PortraitData>(portraitUrl(avatar, `/emotions/${encodeURIComponent(id)}`), {
      method: 'DELETE',
    }),

  // «Показать на рабочем столе»: портрет на столе сменит выражение лица
  showPortraitEmotion: (avatar: string, id: string) =>
    request<{ ok: boolean }>(portraitUrl(avatar, `/emotions/${encodeURIComponent(id)}/show`), {
      method: 'POST',
    }),

  // Анимации портрета на рабочем столе (app/api/portrait_anim.py): свои
  // листы кадров; id 'new' — новая (id выдаёт ядро)
  getPortraitAnims: (avatar: string, atlas = true) =>
    request<PortraitAnims>(portraitUrl(avatar, '/anims', atlas ? {} : { atlas: '0' })),

  putPortraitAnim: (avatar: string, id: string, body: AnimUpdate) =>
    request<PortraitAnims>(portraitUrl(avatar, `/anims/${encodeURIComponent(id)}`), {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  deletePortraitAnim: (avatar: string, id: string) =>
    request<PortraitAnims>(portraitUrl(avatar, `/anims/${encodeURIComponent(id)}`), { method: 'DELETE' }),

  putPortraitAnimSettings: (avatar: string, settings: AnimSettings) =>
    request<PortraitAnims>(portraitUrl(avatar, '/anims-settings'), {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),

  // «Проиграть на рабочем столе»: портрет получит её лентой реплик
  playPortraitAnim: (avatar: string, id: string) =>
    request<{ ok: boolean }>(portraitUrl(avatar, `/anims/${encodeURIComponent(id)}/play`), { method: 'POST' }),

  // Редактор кадров (app/api/portrait_frames.py): источник — лист портрета
  // или анимация; cutFrames — нарезка на пробу, без записи на диск
  getFrames: (avatar: string, kind: FrameKind, id: string) =>
    request<FrameSourceData>(framesPath(avatar, kind, id)),

  getFrameSheet: async (avatar: string, kind: FrameKind, id: string): Promise<Blob> => {
    const res = await apiFetch(framesPath(avatar, kind, id, '/sheet'), { headers: authHeader() });
    if (!res.ok) {
      noteAuthFailure(res);
      throw new ApiError(res.status, `HTTP ${res.status}`);
    }
    return res.blob();
  },

  cutFrames: (avatar: string, kind: FrameKind, id: string, cut: FrameCut) =>
    request<FrameAtlas>(framesPath(avatar, kind, id, '/cut'), { method: 'POST', body: JSON.stringify(cut) }),

  putFrames: (avatar: string, kind: FrameKind, id: string, body: FrameCut & { fix: FrameFix; timing: FrameTiming | null; order?: number[] | null }) =>
    request<FrameSourceData>(framesPath(avatar, kind, id), { method: 'PUT', body: JSON.stringify(body) }),

  // «Заглянул в комнату» (glance) или погладили живой портрет (pet): до LLM
  // доходит, только если разрешено фичей и лимитом
  pokeRoom: (persona: string, chatId: string = 'auto', kind: 'glance' | 'pet' = 'glance') =>
    request<{ ok: boolean; delivered: boolean }>(`/api/personas/${encodeURIComponent(persona)}/room/poke`, {
      method: 'POST',
      body: JSON.stringify({ chat_id: chatId, kind }),
    }),

  // Совместная работа: start/end; на end — короткая реплика персоны (или null)
  roomFocus: (persona: string, action: 'start' | 'end', minutes?: number, chatId: string = 'auto') =>
    request<{ ok?: boolean; focus?: RoomFocus; source?: RoomSource; elapsed_min?: number; line?: string | null }>(`/api/personas/${encodeURIComponent(persona)}/room/focus`, {
      method: 'POST',
      body: JSON.stringify({ action, chat_id: chatId, ...(minutes != null ? { minutes } : {}) }),
    }),

  getLearning: (persona: string) =>
    request<{ sessions: LearningSessionApi[] }>(
      `/api/personas/${encodeURIComponent(persona)}/learning?chat_id=${WEB_CHAT_ID}`,
    ),

  startLearning: (persona: string, subject: string, intervalSeconds: number) =>
    request<{ sessions: LearningSessionApi[] }>(`/api/personas/${encodeURIComponent(persona)}/learning`, {
      method: 'POST',
      body: JSON.stringify({ subject, interval_seconds: intervalSeconds, chat_id: WEB_CHAT_ID }),
    }),

  stopLearning: (persona: string, sessionId: string) =>
    request<{ sessions: LearningSessionApi[] }>(
      `/api/personas/${encodeURIComponent(persona)}/learning?session_id=${encodeURIComponent(sessionId)}&chat_id=${WEB_CHAT_ID}`,
      { method: 'DELETE' },
    ),

  // Сводка для главной: состояние персон, лента «пока вас не было», телеметрия
  // (бэкенд читает файлы персон, ботов не поднимает — app/api/home_api.py)
  getHome: () => request<HomeOverview>(`/api/home?chat_id=${WEB_CHAT_ID}`),

  getDiary: (persona: string) =>
    request<DiaryData>(`/api/personas/${encodeURIComponent(persona)}/diary`),

  // Живая персона: текущее состояние (energy/mood/pastime/location),
  // сюжетные линии и лента последних событий (ui_room_mood_sync)
  getLivingState: (persona: string) =>
    request<LivingStateData>(`/api/personas/${encodeURIComponent(persona)}/state?chat_id=${WEB_CHAT_ID}`),

  getInitiative: (persona: string) =>
    request<InitiativeData>(`/api/personas/${encodeURIComponent(persona)}/initiative?chat_id=${WEB_CHAT_ID}`),

  updateInitiative: (persona: string, patch: Partial<InitiativeParams>) =>
    request<{ ok: boolean; updated: Record<string, unknown> }>(
      `/api/personas/${encodeURIComponent(persona)}/initiative`,
      { method: 'PUT', body: JSON.stringify(patch) },
    ),

  // pending — реплики, которые персона сейчас обрабатывает, отправленные с
  // других устройств (в историю они лягут вместе с ответом)
  getInbox: (persona: string, focused?: boolean) =>
    request<{ messages: InboxMessage[]; generating?: boolean; pending?: PendingReply[]; last_ts?: number; control_mode?: boolean; phone_mode?: boolean; answer_options?: AnswerOptions | null; task?: TaskCard | null; avatar_mode?: string }>(
      `/api/personas/${encodeURIComponent(persona)}/inbox?chat_id=${WEB_CHAT_ID}&client_id=${inboxClientId()}${focused ? '&focused=1' : ''}`,
    ),

  // Долгий опрос: у каких персон что-то изменилось после номера since
  // (app/api/chat_events). personas null — опросить всех; без since — сразу
  waitChanges: (since: number | null, epoch: string, wait: number, signal: AbortSignal) =>
    request<{ version: number; epoch: string; personas: string[] | null }>(
      `/api/changes?${since == null ? '' : `since=${since}&epoch=${encodeURIComponent(epoch)}&wait=${wait}`}`,
      { signal },
    ),

  // Лента реплик персон (app/api/said) — долгий опрос: реплики и пауза до
  // ответа (heard — ваше сообщение, settled — ход кончился). Первый запрос —
  // без since: сразу, только номер. emo — и эмоции лица (kind emo, текст — id)
  waitSaid: (since: number | null, epoch: string, wait: number, signal: AbortSignal, emo = false) =>
    request<{ messages: { persona: string; text: string; kind: string }[]; cursor: number; epoch: string }>(
      `/api/said?chat_id=${WEB_CHAT_ID}${emo ? '&emo=1' : ''}${since == null ? '' : `&since=${since}&epoch=${encodeURIComponent(epoch)}&wait=${wait}`}`,
      { signal },
    ),

  // Состояние вкладки чата (видима и в фокусе) — гейт фоновой активности бота
  // ПО ЭТОМУ чату: persona + chat_id, иначе одна вкладка морозила бы фон
  // всех персон и всех чатов (см. app/core/presence.py)
  setPresence: (active: boolean, persona: string) =>
    request<{ ok: boolean }>('/api/presence', {
      method: 'POST',
      body: JSON.stringify({ active, persona, chat_id: WEB_CHAT_ID }),
    }),

  // ── Настройки: провайдеры и конфиг персоны ──

  getProviders: () => request<{
    providers: ProviderInfo[];
    active: string | null;
    webchat_site: string | null; // первый из webchat_sites (совместимость)
    webchat_sites: string[]; // включённые веб-чаты в порядке перебора
    webchat_options: string[];
  }>('/api/providers'),

  getLocalStatus: () =>
    request<LocalStatus>('/api/providers/local/status'),

  // Gemma телефона (app/core/phone_llm): долгий опрос задачи и её ответ
  phoneLlmNext: (q: { worker: string; wait: number; backend: string; loaded: boolean; name: string }, signal: AbortSignal) =>
    request<{ job: PhoneLlmJob | null; enabled: boolean }>(
      `/api/phone-llm/next?worker=${encodeURIComponent(q.worker)}&wait=${q.wait}` +
        `&backend=${encodeURIComponent(q.backend)}&loaded=${q.loaded}&name=${encodeURIComponent(q.name)}`,
      { signal },
    ),

  phoneLlmResult: (r: { worker: string; id: string; text?: string; error?: string; backend?: string; ms?: number; release?: boolean }) =>
    request<{ ok: boolean }>('/api/phone-llm/result', { method: 'POST', body: JSON.stringify(r) }),

  setGemmaDevice: (device: GemmaDevice) =>
    request<{ ok: boolean; device: GemmaDevice; phone: PhoneGemmaStatus }>('/api/providers/local/device', {
      method: 'PUT',
      body: JSON.stringify({ device }),
    }),

  // Веб-чаты как провайдеры без API-ключей: список сайтов в порядке
  // перебора (ключи из webchat_options), [] — выключить
  setWebchat: (sites: string[]) =>
    request<{ ok: boolean; webchat_site: string | null; webchat_sites: string[] }>('/api/providers/webchat', {
      method: 'PUT',
      body: JSON.stringify({ sites }),
    }),

  // Проба веб-чата из общих настроек: «test» в свежий чат; ok — сайт ответил.
  // Ответ может идти до ~90с (сайт думает) — fetch без своего таймаута
  testWebchat: (site: string) =>
    request<{ ok: boolean; latency_sec?: number; preview?: string; error?: string }>(
      '/api/providers/webchat/test', { method: 'POST', body: JSON.stringify({ site }) }),

  // Движки служебных задач персоны (классификаторы, тики жизни и т.п.):
  // Ollama или веб-чат по каждой задаче + веб-чат фоновых задач
  getPersonaLocalTasks: (persona: string) =>
    request<PersonaLocalTasks>(`/api/personas/${encodeURIComponent(persona)}/local-tasks`),

  // task + backend: 'ollama' | 'webchat' (site — сайт задачи, null — веб-чат
  // фоновых задач) | 'default' — снять выбор; bg_site: 'fallback' | 'primary' | сайт
  updatePersonaLocalTasks: (persona: string, patch: {
    task?: string;
    backend?: 'ollama' | 'webchat' | 'default';
    site?: string | null;
    bg_site?: string;
  }) =>
    request<PersonaLocalTasks & { ok: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/local-tasks`,
      { method: 'PUT', body: JSON.stringify(patch) },
    ),

  // ── Местоположение и погода (строка окружения в контекст персон) ──

  getLocation: () => request<LocationConfig>('/api/settings/location'),

  setLocation: (cfg: LocationConfig) =>
    request<LocationConfig>('/api/settings/location', {
      method: 'POST',
      body: JSON.stringify(cfg),
    }),

  getEnvPreview: () =>
    request<{ line: string | null; location: LocationConfig }>('/api/settings/env-preview'),

  // ── Часовой пояс (напоминания/ритм/проактивность считают время по нему) ──

  getTimezone: () => request<TimezoneConfig>('/api/settings/timezone'),

  // Пустая строка — сброс на системный пояс; невалидное имя зоны бэкенд
  // отклоняет 422-м (ApiError.detail — текст причины)
  setTimezone: (timezone: string) =>
    request<TimezoneConfig>('/api/settings/timezone', {
      method: 'PUT',
      body: JSON.stringify({ timezone }),
    }),

  // ── Экономия нейронки: где вместо вызова модели работает код (все персоны) ──

  getLlmEconomy: () => request<LlmEconomy>('/api/settings/llm-economy'),

  // Частичная правка: только переданные пункты; ответ — всё состояние
  setLlmEconomy: (items: Record<string, boolean>) =>
    request<LlmEconomy>('/api/settings/llm-economy', {
      method: 'PUT',
      body: JSON.stringify({ items }),
    }),

  // ── Скорость текста у аватара (облачко портрета на столе и виджета) ──

  getAvatarText: () => request<AvatarText>('/api/settings/avatar-text'),

  // Частичная правка; ответ — всё состояние
  setAvatarText: (patch: { words_per_sec?: number; part_sec?: number }) =>
    request<AvatarText>('/api/settings/avatar-text', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  // ── Как выглядит веб на компьютере (портрет на столе рисует голову скином) ──

  // Выбор целиком: общий скин, свои темы персон, схема; ответ — сохранённое
  setUiLook: (look: UiLook) =>
    request<UiLook>('/api/settings/ui-look', {
      method: 'PUT',
      body: JSON.stringify(look),
    }),

  // Логи ядра (режим разработчика): инкрементальная выборка по seq
  getLogs: (since: number) =>
    request<{ entries: LogEntry[]; latest: number }>(`/api/logs?since=${since}`),

  // Загрузка RAM/свопа хоста (виджет режима разработчика)
  getSysMemory: () => request<SysMemory>('/api/system/memory'),

  addProviderKey: (provider: string, key: string) =>
    request<{ ok: boolean; keys_count: number }>(`/api/providers/${encodeURIComponent(provider)}/keys`, {
      method: 'POST',
      body: JSON.stringify({ key }),
    }),

  deleteProviderKey: (provider: string, index: number) =>
    request<{ ok: boolean; keys_count: number }>(
      `/api/providers/${encodeURIComponent(provider)}/keys/${index}`,
      { method: 'DELETE' },
    ),

  setActiveProvider: (provider: string) =>
    request<{ ok: boolean; active: string }>('/api/providers/active', {
      method: 'POST',
      body: JSON.stringify({ provider }),
    }),

  setProviderModel: (provider: string, model: string) =>
    request<{ ok: boolean; provider: string; model: string }>(
      `/api/providers/${encodeURIComponent(provider)}/model`,
      { method: 'PUT', body: JSON.stringify({ model }) },
    ),

  // Аватары персон хранятся на сервере (data/api_<id>/avatar.*) — общие для всех
  // браузеров. Отдаются data-URL-ами одним запросом: <img src> на эндпоинт не
  // прошёл бы авторизацию (Bearer-токен идёт только в заголовке fetch)
  getPersonaAvatars: () => request<{ avatars: Record<string, string> }>('/api/persona-avatars'),

  setPersonaAvatar: (persona: string, dataUrl: string) =>
    request<{ ok: boolean }>(`/api/personas/${encodeURIComponent(persona)}/avatar`, {
      method: 'PUT',
      body: JSON.stringify({ data_url: dataUrl }),
    }),

  deletePersonaAvatar: (persona: string) =>
    request<{ ok: boolean }>(`/api/personas/${encodeURIComponent(persona)}/avatar`, { method: 'DELETE' }),

  // ── Библиотека скинов (data/skins/ на сервере, общая для всех браузеров) ──

  // Метаданные всех скинов, без HTML
  // hidden_builtins — встроенные скины, удалённые (скрытые) из библиотеки
  getSkins: () => request<{ skins: ApiSkinMeta[]; hidden_builtins?: string[] }>('/api/skins'),

  // Вернуть скрытые встроенные скины в библиотеку
  restoreBuiltinSkins: () => request<{ restored: string[] }>('/api/skins/builtins/restore', { method: 'POST' }),

  // Скин целиком: метаданные + {sha256: html} файлов экранов
  getSkin: (id: string) =>
    request<{ skin: ApiSkinMeta; files: Record<string, string> }>(`/api/skins/${encodeURIComponent(id)}`),

  createSkin: (body: ApiSkinCreate) =>
    request<{ skin: ApiSkinMeta }>('/api/skins', { method: 'POST', body: JSON.stringify(body) }),

  updateSkin: (id: string, patch: ApiSkinUpdate) =>
    request<{ skin: ApiSkinMeta }>(`/api/skins/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  // Удаление снимает скин со всех персон; unassigned — с каких сняли.
  // Встроенный скин (builtin-…) не удаляется, а скрывается из библиотеки
  deleteSkin: (id: string) =>
    request<{ ok: boolean; unassigned: string[] }>(`/api/skins/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  getSkinAssignments: () => request<{ assignments: Record<string, string> }>('/api/skin-assignments'),

  // skinId=null — снять скин с персоны
  setPersonaSkin: (persona: string, skinId: string | null) =>
    request<{ ok: boolean }>(`/api/personas/${encodeURIComponent(persona)}/skin`, {
      method: 'PUT',
      body: JSON.stringify({ skin_id: skinId }),
    }),

  // Арт-направления для генерации скина нейросетью (короткий вызов LLM)
  skinDirections: (body: ApiSkinDirectionRequest, signal?: AbortSignal) =>
    request<ApiSkinDirections>('/api/skins/direction', { method: 'POST', body: JSON.stringify(body), signal }),

  // Цвет метки персоны (строка color: в YAML); null — вернуть цвет по умолчанию
  setPersonaColor: (persona: string, color: string | null) =>
    request<{ ok: boolean; color: string }>(`/api/personas/${encodeURIComponent(persona)}/color`, {
      method: 'PUT',
      body: JSON.stringify({ color }),
    }),

  // Смена id персоны: файл YAML, папка памяти data/api_<id>, аватар и ссылки на id
  renamePersona: (persona: string, newId: string, memory?: MemoryChoice) =>
    request<{ ok: boolean; persona: string; restart_required: boolean }>(`/api/personas/${encodeURIComponent(persona)}/rename`, {
      method: 'POST',
      body: JSON.stringify({ new_id: newId, memory }),
    }),

  getPersonaYaml: (persona: string) =>
    request<{ persona: string; yaml: string }>(`/api/personas/${encodeURIComponent(persona)}/yaml`),

  savePersonaYaml: (persona: string, yaml: string) =>
    request<{ ok: boolean; restart_required: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/yaml`,
      { method: 'PUT', body: JSON.stringify({ yaml }) },
    ),

  getPersonaConfig: (persona: string) =>
    request<PersonaConfig>(`/api/personas/${encodeURIComponent(persona)}/config`),

  updatePersonaConfig: (persona: string, patch: {
    settings?: Record<string, number | boolean>; // числа + bool-флаги (split_messages)
    stm_size?: number;
    features?: Record<string, unknown>; // bool-флаги и dict-фичи (proactive/learning с enabled)
    llm?: {
      primary?: string | null;
      fallback?: string[];
      exclude?: string[]; // убранные из цепочки персоны; [] — снять
      models?: Record<string, string>;
      answer_provider?: string | null;
      cc_provider?: string | null;
      vision_provider?: string | null;
      webchat_limits?: Record<string, { enabled: boolean; per_hour?: number }>;
    };
  }) =>
    request<{ status: string; restart_required: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/config`,
      { method: 'PUT', body: JSON.stringify(patch) },
    ),

  // ── Черновики новых персон (модалка создания) ──

  getPersonaDrafts: () => request<{ drafts: PersonaDraft[] }>('/api/persona-drafts'),

  // id=null/undefined → создать новый черновик, иначе обновить существующий
  savePersonaDraft: (draft: { id?: string | null; name: string; form: Record<string, unknown>; yaml: string }) =>
    request<PersonaDraft>('/api/persona-drafts', { method: 'POST', body: JSON.stringify(draft) }),

  deletePersonaDraft: (id: string) =>
    request<{ status: string }>(`/api/persona-drafts/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // ── Файлы (документы для поиска по персоне) ──

  getFiles: (persona: string) =>
    request<{ files: FileEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/files`),

  uploadFile: (persona: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<{ status: string; filename: string; files: FileEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/files`,
      { method: 'POST', body: form },
    );
  },

  deleteFile: (persona: string, filename: string) =>
    request<{ files: FileEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/files/${encodeURIComponent(filename)}`,
      { method: 'DELETE' },
    ),

  getFileContent: (persona: string, filename: string) =>
    request<{ filename: string; content: string }>(
      `/api/personas/${encodeURIComponent(persona)}/files/${encodeURIComponent(filename)}/content`,
    ),

  // ── Режимы персоны (досье → «Режимы», окно «Редактировать») ──

  getPersonaPhrases: (persona: string) =>
    request<PersonaPhraseBank>(`/api/personas/${encodeURIComponent(persona)}/phrases`),

  savePersonaPhrases: (persona: string, group: PersonaPhraseGroup, variants: string[]) =>
    request<{ ok: boolean; group: PersonaPhraseGroup }>(
      `/api/personas/${encodeURIComponent(persona)}/phrases`,
      { method: 'PUT', body: JSON.stringify({ group_id: group.id, revision: group.revision, variants }) },
    ),

  getPersonaModes: (persona: string) =>
    request<PersonaModesList>(`/api/personas/${encodeURIComponent(persona)}/modes`),

  getPersonaModeYaml: (persona: string, mode: string) =>
    request<{ mode: string; yaml: string; builtin: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/modes/${encodeURIComponent(mode)}`,
    ),

  savePersonaMode: (persona: string, mode: string, yaml: string, create = false) =>
    request<PersonaModesList>(`/api/personas/${encodeURIComponent(persona)}/modes/${encodeURIComponent(mode)}`, {
      method: 'PUT',
      body: JSON.stringify({ yaml, create }),
    }),

  uploadPersonaMode: (persona: string, file: File, opts: { mode?: string; overwrite?: boolean } = {}) => {
    const form = new FormData();
    form.append('file', file);
    const q = new URLSearchParams();
    if (opts.mode) q.set('mode', opts.mode);
    if (opts.overwrite) q.set('overwrite', 'true');
    const qs = q.toString();
    return request<PersonaModesList>(`/api/personas/${encodeURIComponent(persona)}/modes${qs ? `?${qs}` : ''}`, {
      method: 'POST',
      body: form,
    });
  },

  deletePersonaMode: (persona: string, mode: string) =>
    request<PersonaModesList>(`/api/personas/${encodeURIComponent(persona)}/modes/${encodeURIComponent(mode)}`, {
      method: 'DELETE',
    }),

  personaModeSkeleton: (persona: string, name: string, lang: string) =>
    request<{ yaml: string; id: string }>(
      `/api/personas/${encodeURIComponent(persona)}/mode-skeleton?name=${encodeURIComponent(name)}&lang=${encodeURIComponent(lang)}`,
    ),

  // ── Лор персоны (история и мир, в промпт — куски к реплике) ──

  getLore: (persona: string) => request<LoreInfo>(`/api/personas/${encodeURIComponent(persona)}/lore`),

  uploadLore: (persona: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<LoreInfo>(`/api/personas/${encodeURIComponent(persona)}/lore`, { method: 'POST', body: form });
  },

  deleteLore: (persona: string, filename: string) =>
    request<LoreInfo>(
      `/api/personas/${encodeURIComponent(persona)}/lore/${encodeURIComponent(filename)}`,
      { method: 'DELETE' },
    ),

  getLoreFile: (persona: string, filename: string) =>
    request<LoreFileView>(
      `/api/personas/${encodeURIComponent(persona)}/lore/${encodeURIComponent(filename)}/view`,
    ),

  // Исходный файл лора (blob: заголовок с токеном простой ссылкой не передать)
  loreOriginal: async (persona: string, filename: string): Promise<Blob> => {
    const res = await apiFetch(
      `/api/personas/${encodeURIComponent(persona)}/lore/${encodeURIComponent(filename)}/original`,
      { headers: authHeader() },
    );
    if (!res.ok) {
      noteAuthFailure(res);
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (typeof body?.detail === 'string') detail = body.detail;
      } catch {
        /* тело не JSON */
      }
      throw new ApiError(res.status, detail);
    }
    return res.blob();
  },

  searchLore: (persona: string, query: string, previous = '') =>
    request<LoreSearchResult>(`/api/personas/${encodeURIComponent(persona)}/lore/search`, {
      method: 'POST',
      body: JSON.stringify({ query, previous }),
    }),
};

export type ApiSkinScreen = 'chat' | 'dossier' | 'room';

// Скин библиотеки на сервере (app/api/skins_api.py)
export interface ApiSkinMeta {
  id: string;
  name: string;
  author: string | null;
  version: string | null;
  contract: number;
  screens: Partial<Record<ApiSkinScreen, string>>; // экран → sha256 файла
  sizes: Partial<Record<ApiSkinScreen, number>>; // байт
  colors: Record<string, string>; // переопределения CSS-переменных
  hue_shift: number;
  created_at: number; // мс
  updated_at: number;
}

// Файлы передаются списком, экраны ссылаются на них индексами: комбинированный
// файл на три экрана уходит по сети один раз
export interface ApiSkinCreate {
  name: string;
  author?: string | null;
  version?: string | null;
  contract?: number;
  files: string[];
  screens: Partial<Record<ApiSkinScreen, number>>;
  colors?: Record<string, string>;
  hue_shift?: number;
}

export interface ApiSkinUpdate {
  name?: string;
  author?: string | null;
  version?: string | null;
  contract?: number;
  files?: string[];
  screens?: Partial<Record<ApiSkinScreen, number | null>>; // null — убрать экран
  colors?: Record<string, string>;
  hue_shift?: number;
}

export interface LoreFile {
  name: string;
  chars: number;
  chunks: number;
  ts: number; // секунды
  sections: { title: string; chunks: number }[];
  original?: number; // байт исходного файла; нет — загружен до того, как их стали хранить
}

// Файл лора для просмотра: текст с заголовками, как его увидела нарезка, и сами куски
export interface LoreFileView {
  name: string;
  chars: number;
  chunks: number;
  ts: number;
  ext: string;
  original: boolean; // исходный файл сохранён — можно открыть (PDF) или скачать
  blocks: { h?: number; t: string }[]; // h — заголовок этого уровня, иначе абзац
  pieces: { s: string; t: string }[]; // s — раздел куска
}

// Режим персоны (<id>_modes/<режим>.yaml): свой характер, память общая
export interface PersonaModeInfo {
  id: string;
  kind: 'trigger' | 'master' | 'mortal'; // master/mortal — по собеседнику
  builtin: boolean; // файл проекта: удалить нельзя, только переписать своим
  overrides_builtin: boolean; // свой поверх встроенного: удаление вернёт встроенный
  name: string;
  description: string;
  names: string[];
  keywords: string[];
  exit_keywords: string[];
  mood: string | null;
  hold_minutes: number | null;
  error: string; // не пусто — режим не действует (YAML не читается, нет system_prompt)
  updated?: number;
}

// GET /api/personas/{p}/portrait/modes (app/api/avatar_modes.overview):
// режим ("" — обычный) → есть ли свой лист A, сколько своих анимаций (без
// переходов) и переходов в него; active — режим основного чата сейчас
export interface PortraitModesOverview {
  modes: Record<string, { head: boolean; anims: number; switches: number }>;
  active: string;
}

export interface PersonaPhraseGroup {
  id: string;
  section: 'errors' | 'kinds' | 'phrases';
  key: string;
  bucket: 'ok' | 'err' | null;
  mode: string;
  mode_name: string;
  language: string;
  variants: string[];
  revision: string;
  edited: boolean;
  stale: boolean;
  placeholders: string[];
  required_placeholders: string[];
  max_length: number;
}

export interface PersonaPhraseBank {
  persona: string;
  groups: PersonaPhraseGroup[];
  generating: boolean;
}

export interface PersonaModesList {
  persona: string;
  modes: PersonaModeInfo[];
  base_names: string[]; // обычные имена персоны — снимают режим, включённый по имени
  folder: string;
  skipped: string[]; // файлы в папке, которые ядро не возьмёт (.yml, id с пробелом)
  saved?: string;
  warnings?: string[];
  reset?: boolean; // удалён свой файл — вернулся встроенный
}

export interface LoreInfo {
  files: LoreFile[];
  chars: number;
  chunks: number;
}

export interface LoreHit {
  file: string;
  section: string;
  text: string;
  similarity: number;
  words: boolean; // совпало редкое слово реплики
}

export interface LoreSearchResult {
  hits: LoreHit[];
  weak: boolean;
  skipped: 'smalltalk' | 'off_topic' | 'no_lore' | null;
}

export interface FileEntry {
  filename: string;
  size: number; // символов полного текста
  timestamp: number; // мс
}

export interface ProviderInfo {
  id: string;
  name: string;
  key_set: boolean;
  keys_count: number;
  keys: string[]; // маскированные ключи (первые/последние 4 символа)
  model: string;
  active: boolean;
  local: boolean;
  device?: GemmaDevice; // строка Ollama: где работает Gemma
  phone?: PhoneGemmaStatus; // строка Ollama: телефон с Gemma
}

// Где работает Gemma локальных задач: Ollama на ноутбуке или приложение на телефоне
export type GemmaDevice = 'pc' | 'phone';

export interface PhoneGemmaStatus {
  device: GemmaDevice;
  online: boolean; // телефон опрашивает ядро
  worker: {
    name: string;
    backend: string; // 'gpu' | 'cpu' | '' (модель ещё не загружалась)
    loaded: boolean;
    seen_sec: number;
    online: boolean;
  } | null; // телефон ещё ни разу не приходил
  pending: number;
}

// Задача Gemma телефона: прогрев (только загрузить модель) или служебный вызов
export interface PhoneLlmJob {
  id: string;
  kind: 'chat' | 'warm';
  system?: string;
  history?: { role: 'user' | 'assistant'; content: string }[];
  text?: string;
  max_tokens?: number;
  temperature?: number;
  task?: string;
  ttl_ms?: number; // сколько ядро ещё ждёт ответ — срок по часам телефона
}

// Свежая проверка локальной Ollama (GET /api/providers/local/status)
export interface LocalStatus {
  server: boolean; // Ollama отвечает на /api/tags
  url: string;
  model: string; // настроенная модель (OLLAMA_MODEL)
  model_present: boolean; // установлена ли она
  models: string[]; // что установлено
  available: boolean; // server && model_present; Gemma на телефоне — телефон на связи
  device?: GemmaDevice;
  phone?: PhoneGemmaStatus;
}

// Служебная задача персоны и её текущий (resolved) движок
export interface LocalTaskInfo {
  id: string;
  backend: 'ollama' | 'webchat';
  sites: string[]; // веб-чаты по порядку попыток (затем откат на Ollama)
  site: string | null; // явно выбранный сайт задачи (null — веб-чат фоновых задач)
  explicit: boolean; // движок выбран пользователем (иначе — дефолт по роду задачи)
  background: boolean; // фоновая: по умолчанию веб-чат; иначе — Ollama
  ollama_only: boolean; // технически не может уйти в веб-чат (OCR)
}

// Движки служебных задач персоны
export interface PersonaLocalTasks {
  bg_site: string; // веб-чат фоновых задач: 'fallback' | 'primary' | сайт
  primary_site: string | null; // основной веб-чат персоны
  fallback_site: string | null; // первый веб-чат её fallback-цепочки
  sites: string[]; // веб-чаты цепочки персоны
  tasks: LocalTaskInfo[];
}

export interface PersonaLlmConfig {
  primary: string | null; // null — глобальный активный провайдер
  fallback: string[]; // приоритет fallback-цепочки (id провайдеров)
  exclude?: string[]; // убранные из цепочки этой персоны (другие не затрагиваются)
  models: Record<string, string>; // свои модели по провайдерам (override глобальной)
  // провайдеры по назначению (null/отсутствует — обычная цепочка):
  answer_provider?: string | null; // реплики персоны в режиме управления (действия, пересказ страницы)
  cc_provider?: string | null; // решения режима управления (разбор/резолв)
  vision_provider?: string | null; // vision-фолбэк (картинки)
  // лимиты веб-чатов: {сайт: {enabled, per_hour}}; сайта нет — без лимита
  webchat_limits: Record<string, { enabled: boolean; per_hour?: number }>;
}

export interface PersonaConfig {
  settings: { temperature?: number; max_tokens?: number; top_p?: number; split_messages?: boolean };
  stm_size: number | null;
  features: Record<string, unknown>;
  llm: PersonaLlmConfig;
}

// Черновик новой персоны (form — непрозрачное состояние формы PersonaCreateModal)
export interface PersonaDraft {
  id: string;
  name: string;
  created_at: number; // unix-секунды
  updated_at: number;
  form: Record<string, unknown>;
  yaml: string;
}

// SSE-стриминг ответа (/api/chat/stream): токены — порции финального текста
// (бэкенд отдаёт reply после постобработки ядра кусками, эффект печати),
// part_break — граница расщеплённого ответа (settings.split_messages):
// следующие токены относятся к новому сообщению; reply_ts (до токенов) —
// серверная метка ответа в STM; photo — кадр устройства ядра («что видит
// робот?»), как только он снят, ещё до ответа (в финальном images он
// придёт снова). Промис резолвится финальным событием done с тем же reply
// (+ extra_messages).
// Живые кадры вкладки агента (режим управления): SSE
// /api/personas/{p}/control/view — JPEG base64 + адрес/заголовок/приватность
// или смена состояния браузера. Режим погас — {status: 'off'} и конец потока
export interface ControlViewEvent {
  frame?: string;
  url?: string;
  title?: string;
  private?: boolean;
  status?: 'no_browser' | 'no_tab' | 'off';
}

export async function streamControlView(
  persona: string,
  onEvent: (e: ControlViewEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await apiFetch(
    `/api/personas/${encodeURIComponent(persona)}/control/view?chat_id=${WEB_CHAT_ID}`,
    { headers: { ...authHeader() }, signal },
  );
  if (!res.ok || !res.body) {
    noteAuthFailure(res);
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split('\n\n');
    buf = frames.pop() ?? '';
    for (const fr of frames) {
      for (const line of fr.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        try {
          onEvent(JSON.parse(line.slice(6)) as ControlViewEvent);
        } catch {
          /* битый кадр — пропускаем */
        }
      }
    }
  }
}

export async function streamChat(
  // fromSkin — реплика из скина: бэкенд не пускает её в режим управления
  // imageSource camera — кадр с камеры: ядро отдаёт его только локальной модели
  // phoneAction — команду уже исполнил телефон (phone/actions.ts): персона отвечает по итогу
  params: { persona: string; message: string; userId?: string; userName?: string; replyContext?: string; image?: string; imageSource?: 'camera'; fromSkin?: boolean; phoneAction?: PhoneActionReport },
  onToken: (text: string) => void,
  onPartBreak?: () => void,
  onReplyTs?: (ts: number) => void,
  onPhoto?: (dataUrl: string) => void,
): Promise<ApiChatResponse> {
  // Сетевой сбой до ответа (fetch отклонён) — запрос мог и дойти до сервера:
  // считаем обрывом, чат сверится с историей по last_ts
  let res: Response;
  try {
    res = await apiFetch('/api/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeader(),
      },
      body: JSON.stringify({
        persona: params.persona,
        message: params.message,
        user_id: params.userId ?? WEB_USER_ID,
        chat_id: WEB_CHAT_ID,
        user_name: params.userName ?? null,
        reply_context: params.replyContext ?? null,
        image: params.image ?? null,
        ...(params.image && params.imageSource === 'camera' ? { image_source: 'camera' } : {}),
        ...(params.fromSkin ? { from_skin: true } : {}),
        ...(params.phoneAction ? { phone_action: params.phoneAction } : {}),
        // Приложение на телефоне: «открой файл X» присылает файл в чат, а не
        // открывает его на компьютере ядра
        ...(isNativeApp() ? { client: 'app' } : {}),
        // См. sendChat: реплику до ответа показывают другие устройства
        client_id: inboxClientId(),
      }),
    });
  } catch (e) {
    throw new StreamInterruptedError(e instanceof Error ? e.message : String(e));
  }
  if (!res.ok || !res.body) {
    noteAuthFailure(res);
    // Достаём detail из JSON-тела ошибки (например, 409 «персона заморожена»)
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* тело не JSON — оставляем HTTP-код */
    }
    throw new ApiError(res.status, detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (e) {
      // Соединение оборвалось посреди стрима
      throw new StreamInterruptedError(e instanceof Error ? e.message : String(e));
    }
    const { done, value } = chunk;
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE-кадры разделены пустой строкой
    const frames = buf.split('\n\n');
    buf = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      const event = JSON.parse(line.slice(6));
      if (event.token) onToken(event.token);
      else if (event.part_break) onPartBreak?.();
      else if (!event.done && typeof event.reply_ts === 'number') onReplyTs?.(event.reply_ts);
      else if (typeof event.photo === 'string') onPhoto?.(event.photo);
      else if (event.error) throw new ApiError(500, event.error);
      else if (event.done) return event as ApiChatResponse;
    }
  }
  throw new StreamInterruptedError('Стрим оборвался без финального события');
}

// Файл, присланный в чат (GET /api/files/{id}), — целиком в память: по VPC
// Link иначе нельзя, а размер ядро ограничивает (50 МБ)
export async function fetchSentFile(file: SentFile): Promise<Blob> {
  if (!/^\/api\/files\/[A-Za-z0-9_-]+$/.test(file.url)) throw new ApiError(400, 'bad file url');
  const res = await apiFetch(file.url, { headers: authHeader() });
  if (!res.ok) {
    noteAuthFailure(res);
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* тело не JSON — оставляем HTTP-код */
    }
    throw new ApiError(res.status, detail);
  }
  return res.blob();
}

/** Import one path from the core computer. Keep credentials in headers. */
export async function importPlayerPath(path: string): Promise<Blob> {
  const res = await apiFetch('/api/player/import-path', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) {
    noteAuthFailure(res);
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch { /* keep HTTP status */ }
    throw new ApiError(res.status, detail);
  }
  return res.blob();
}

export interface BrowserPlayerStatus {
  source?: 'youtube' | 'youtube_music' | 'local' | 'library';
  running: boolean;
  state: 'stopped' | 'waiting' | 'playing' | 'paused' | 'buffering' | 'error';
  headless: boolean;
  title: string;
  url: string;
  position: number;
  duration: number;
  volume: number;
  muted: boolean;
  error: string;
  playlist_id?: string | null;
  playlist_name?: string | null;
  queue_index?: number | null;
  queue_length?: number | null;
  save_error?: string;
}

export function browserPlayerStatus(): Promise<BrowserPlayerStatus> {
  return request('/api/player/browser');
}

export type MusicProvider = 'youtube' | 'youtube_music';
export function getPlayerSettings(): Promise<{ provider: MusicProvider }> {
  return request('/api/player/settings');
}
export function setPlayerProvider(provider: MusicProvider): Promise<{ provider: MusicProvider }> {
  return request('/api/player/settings', { method: 'PUT', body: JSON.stringify({ provider }) });
}
export function playSong(query: string, provider: MusicProvider, headless: boolean, volume: number): Promise<BrowserPlayerStatus> {
  return request('/api/player/play', { method: 'POST', body: JSON.stringify({ query, provider, headless, volume }) });
}

export function playBrowserMusic(url: string, headless: boolean, volume: number): Promise<BrowserPlayerStatus> {
  return request('/api/player/browser/play', { method: 'POST', body: JSON.stringify({ url, headless, volume }) });
}

export interface SavedSong {
  id: string; url: string; title: string; source: MusicProvider; origin: string; created: number;
}
export interface MusicPlaylist { id: string; name: string; track_ids: string[]; created: number }
export interface MusicCatalog { revision: number; tracks: SavedSong[]; playlists: MusicPlaylist[]; playlist_id?: string }
export function getMusicCatalog(): Promise<MusicCatalog> { return request('/api/player/saved'); }
export function saveSongLink(url: string, title = '', playlist_id?: string): Promise<MusicCatalog> {
  return request('/api/player/saved', { method: 'POST', body: JSON.stringify({ url, title, playlist_id }) });
}
export function deleteSongLink(id: string): Promise<MusicCatalog> { return request(`/api/player/saved/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
export function createMusicPlaylist(name: string): Promise<MusicCatalog> { return request('/api/player/playlists', { method: 'POST', body: JSON.stringify({ name }) }); }
export function updateMusicPlaylist(id: string, change: { name?: string; track_ids?: string[]; add_track_id?: string; remove_track_id?: string }): Promise<MusicCatalog> {
  return request(`/api/player/playlists/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(change) });
}
export function deleteMusicPlaylist(id: string): Promise<MusicCatalog> { return request(`/api/player/playlists/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
export function playMusicPlaylist(playlist_id: string | undefined, track_id: string | undefined, headless: boolean, volume: number): Promise<BrowserPlayerStatus> {
  return request('/api/player/playlists/play', { method: 'POST', body: JSON.stringify({ playlist_id, track_id, headless, volume }) });
}

export type PlayerCommand = 'pause' | 'resume' | 'stop' | 'volume' | 'volume_up' | 'volume_down' | 'seek' | 'next' | 'previous';
export function controlBrowserMusic(command: PlayerCommand, value?: number): Promise<BrowserPlayerStatus> {
  return request('/api/player/browser/control', { method: 'POST', body: JSON.stringify({ command, value }) });
}

export function updateLibraryPlayer(client: string, status: Pick<BrowserPlayerStatus, 'state' | 'title' | 'position' | 'duration' | 'volume'>,
  activate: boolean, ack?: string, error?: string): Promise<{ active: boolean; commands: { id: string; command: PlayerCommand; value?: number }[] }> {
  return request('/api/player/library', { method: 'POST', body: JSON.stringify({ client, status, activate, ack, error }) });
}

// Арт-направление скина (POST /api/skins/direction): сюжет, палитра с ролями,
// системные шрифтовые стеки, раскладка, фирменный элемент. Выбранное уходит
// с каждым запросом генерации экрана — все экраны собираются из него
export interface ApiSkinDirectionColor {
  role: string; // background | surface | ink | muted | accent | accent2 | extra
  hex: string; // #rrggbb
  reason: string;
}

export interface ApiSkinDirection {
  name: string;
  subject: string;
  mood: string;
  palette: ApiSkinDirectionColor[];
  fonts: { display: string; text: string; mono: string; why: string }; // CSS-стеки font-family (mono может быть пуст)
  layout: string;
  signature: string;
  texture: string;
  motion: string;
  avoid: string[];
  note?: string; // заметка пользователя к выбранному направлению
}

export interface ApiSkinDirections {
  directions: ApiSkinDirection[];
  provider: string | null;
  model: string | null;
}

export interface ApiSkinDirectionRequest {
  description: string;
  locale: 'ru' | 'en';
  count?: number; // 1–3
  exclude?: string[]; // названия уже показанных направлений
  provider?: string | null;
  model?: string | null;
}

// Генерация файла экрана скина нейросетью (POST /api/skins/generate, SSE)
export interface ApiSkinGenRequest {
  screen: ApiSkinScreen;
  description: string;
  base_html: string;
  base_kind: 'template' | 'skin';
  contract_doc: string;
  locale: 'ru' | 'en';
  previous_html?: string | null; // итерация исправления: прошлый ответ модели
  errors?: string[]; // …и ошибки проверки
  provider?: string | null; // null — обычная цепочка провайдеров
  model?: string | null;
  // Выбранное арт-направление; null на первом запросе — сервер подберёт
  // направление сам и пришлёт его событием status=direction
  direction?: ApiSkinDirection | null;
}

export interface ApiSkinGenResult {
  html: string;
  truncated: boolean; // ответ оборван лимитом вывода (нет </html>) и после продолжений
  chars: number;
  provider: string | null;
  model: string | null;
  elapsed: number; // сек
}

// Ошибка из SSE-потока генерации: code — busy (идёт другая генерация) или
// no_html (модель ответила не документом; raw — начало ответа)
export class SkinGenStreamError extends ApiError {
  code: string | null;
  raw: string | null;
  constructor(detail: string, code: string | null, raw: string | null) {
    super(code === 'busy' ? 409 : 500, detail);
    this.code = code;
    this.raw = raw;
  }
}

// Прогресс: символов ответа получено и секунд прошло; status — служебные
// этапы сервера (started, retry_small_limit, continue — ответ оборван лимитом
// вывода и дописывается, info.round — номер продолжения; direction_start /
// direction / direction_failed — сервер сам подбирает арт-направление,
// info.direction — подобранное). Отмена — через signal
export async function streamSkinGeneration(
  params: ApiSkinGenRequest,
  onProgress: (chars: number, elapsed: number) => void,
  onStatus?: (status: string, info: { round?: number; direction?: ApiSkinDirection }) => void,
  signal?: AbortSignal,
): Promise<ApiSkinGenResult> {
  const res = await apiFetch('/api/skins/generate', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
    },
    body: JSON.stringify(params),
    signal,
  });
  if (!res.ok || !res.body) {
    noteAuthFailure(res);
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* тело не JSON — оставляем HTTP-код */
    }
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split('\n\n');
    buf = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      const event = JSON.parse(line.slice(6));
      if (event.error) throw new SkinGenStreamError(event.error, event.code ?? null, event.raw ?? null);
      if (event.done) return event as ApiSkinGenResult;
      if (typeof event.progress === 'number') onProgress(event.progress, event.elapsed ?? 0);
      else if (event.status)
        onStatus?.(event.status, {
          round: typeof event.round === 'number' ? event.round : undefined,
          direction: event.direction && typeof event.direction === 'object' ? event.direction : undefined,
        });
    }
  }
  throw new StreamInterruptedError('Стрим генерации скина оборвался без результата');
}

export interface InboxMessage {
  text: string;
  kind: string; // message | document
  ts: number;
  // Метка срабатывания напоминания («id@время») — будильник телефона её знает
  ref?: string;
}

// Реплика с другого устройства, на которую персона сейчас отвечает
export interface PendingReply {
  text: string;
  ts: number;
  image?: boolean; // с картинкой
}

export interface TodoEntry {
  index: number;
  user_name: string;
  task: string;
}

export interface ReminderEntry {
  index: number;
  id: string; // стабильный id напоминания (reminder_manager)
  task: string;
  trigger_at: number | null;
  // Повтор: daily — каждый день; weekly — по дням weekdays (0 — пн … 6 — вс;
  // у старых записей один день — в weekday)
  recurrence: { type: 'daily' | 'weekly'; hour: number; minute: number; weekday?: number | null; weekdays?: number[] | null } | null;
  user_name: string;
  // false — на паузе: не сработает, пока не продолжат (у старых серверов поля нет)
  active?: boolean;
}

// ── Общий календарь ──
export type CalendarKind = 'todo' | 'reminder' | 'note' | 'event';

export interface CalendarEntry {
  id: string;
  title: string;
  date: string; // YYYY-MM-DD
  time: string | null; // HH:MM
  kind: CalendarKind;
  persona: string | null; // id персоны-владельца записи
  persona_name: string | null;
  color: string | null; // цвет персоны-владельца (метка в сетке)
  note: string;
  done: boolean;
  created_at: number | null;
  // source='reminder' — активное напоминание персоны (из reminder_manager),
  // попадает в календарь автоматически; удаляется через /personas/{p}/reminders
  source: 'calendar' | 'reminder';
  readonly: boolean;
  recurrence?: ReminderEntry['recurrence'];
}

// Повтор при создании/правке напоминания (hour/minute — по часам пользователя)
export interface ReminderRecurrenceInput {
  type: 'daily' | 'weekly';
  weekdays?: number[];
  hour?: number;
  minute?: number;
}

export interface InventoryEntry {
  name: string;
  description: string;
  acquired: string;
  source: string;
  tags: string[];
}

export interface LearningSessionApi {
  session_id: string;
  subject: string;
  active: boolean;
  lesson_count: number;
  covered_topics: string[];
  learned_vocabulary: string[];
  next_lesson_at: number | null;
  interval_seconds: number;
  quiz_pending: boolean;
}

// Сводка главной страницы (GET /api/home)
export interface HomeEvent {
  kind: 'initiative' | 'diary';
  text: string;
  ts: number; // unix-секунды
  type?: string;
}

export interface HomePersonaOverview {
  last_user_ts: number | null; // последнее сообщение оператора в веб-чате
  // Последняя реплика веб-чата (кто и что, текст обрезан) — превью на странице всех чатов
  last_message: { role: 'user' | 'bot'; text: string; ts: number } | null;
  state: { pastime: string; location: string; mood: string; energy: number | null; updated_at: number | null } | null;
  events: HomeEvent[]; // свежие сверху
  reminders_active: number;
  next_reminder: { text: string; ts: number } | null;
  initiatives_today: number;
  initiatives_max: number;
  ltm_facts: number;
  // Заметки: у старых серверов полей нет
  notes_enabled?: boolean;
  notes_count?: number;
}

// ── Сервисы ──
export type ServiceLevel = 'urgent' | 'notify' | 'digest' | 'ignore';
export type ServiceSource = 'gmail' | 'drive' | 'github' | 'telegram' | 'imap' | 'rss' | 'webhook';

export interface ServiceRule {
  id: string;
  action: 'urgent' | 'always' | 'digest' | 'never';
  source: ServiceSource | 'any';
  match: string;
  created_at: number;
}

export interface ServicesNotify {
  persona: string | null;
  channels: { web: boolean; telegram: boolean };
  style: 'plain' | 'persona';
  engine: 'local' | 'persona' | 'rules';
  interests: string;
  quiet: { enabled: boolean; from: string; to: string; urgent: boolean };
  digest_times: string[];
  max_per_hour: number;
  language: 'auto' | 'ru' | 'en';
}

export interface ServicesSettings {
  notify: ServicesNotify;
  services: {
    gmail: { enabled: boolean; interval: number };
    drive: { enabled: boolean; interval: number; shared: boolean; edits: boolean };
    github: { enabled: boolean; interval: number };
    telegram: {
      enabled: boolean;
      private: boolean;
      mentions: boolean;
      groups: boolean;
      channels: boolean;
      muted: 'digest' | 'ignore' | 'normal';
      skip_read: boolean;
    };
    imap: { enabled: boolean; interval: number };
    rss: { enabled: boolean; interval: number; feeds: { url: string; title: string }[] };
    webhook: { enabled: boolean };
  };
  rules: ServiceRule[];
  examples: { source: string; sender: string; title: string; level: ServiceLevel; ts: number }[];
  dnd_until: number;
}

export interface ServiceState {
  last_ok: number | null;
  last_error: string;
  auth_error: string;
}

export interface ServicesStatus {
  hub: { started: boolean; active: boolean };
  settings: ServicesSettings;
  personas: { id: string; name: string; muted: boolean }[];
  persona: string | null;
  telegram_delivery: { token: boolean; owner: boolean };
  accounts: {
    google: {
      client: boolean;
      client_id: string;
      connected: boolean;
      email: string;
      gmail: boolean;
      drive: boolean;
      auth_error: string;
    };
    github: { connected: boolean; login: string; token_hint: string };
    telegram: {
      available: boolean;
      connected: boolean;
      user: { id: number; name: string; username: string } | null;
      api_id: boolean;
      login_step: 'code' | null;
      running: boolean | null;
    };
    imap: { connected: boolean; user: string; host: string; port: number; folder: string };
    webhook: { token: boolean; path: string };
  };
  status: Record<'gmail' | 'drive' | 'github' | 'telegram' | 'imap' | 'rss', ServiceState>;
  counts: { queued: number; digest: number; today: number; notified_today: number };
  now: number;
}

export interface ServiceFeedItem {
  id: string;
  source: ServiceSource;
  kind: string;
  title: string;
  sender: string;
  text: string;
  url: string;
  ts: number;
  received: number;
  group: string;
  level: ServiceLevel | null;
  summary: string;
  reason: string;
  by: 'rule' | 'signals' | 'model' | '';
  status: 'new' | 'queued' | 'digest' | 'notified' | 'digested' | 'ignored' | 'skipped';
  notified_at: number | null;
  feedback: ServiceLevel | null;
}

export interface ServiceCheck {
  ok: boolean;
  error?: string;
  account?: string;
  auth?: boolean;
  feeds?: { url: string; ok: boolean; title?: string; items?: number; error?: string }[];
}

export interface ServiceLoginStep {
  ok: boolean;
  next?: 'code' | 'password';
  error?: string;
  user?: { id: number; name: string; username: string };
}

// Заметка пользователя («запиши в заметки …» в чате или блок на главной)
export interface NoteEntry {
  id: string;
  text: string;
  created_at: number; // unix-секунды
  updated_at: number | null;
  author_name: string;
  owner: boolean; // записал владелец (в чате или здесь); иначе — другой собеседник
  source: 'chat' | 'home';
}

export interface HomeOverview {
  now: number;
  personas: Record<string, HomePersonaOverview>;
}

export interface DiaryData {
  episodes: { text: string; timestamp: string; msg_count?: number }[];
  notes: { text: string; timestamp: string; user_id?: string }[];
  life_summary: string;
}

// Редактируемые параметры проактивности (PUT /initiative)
export interface InitiativeParams {
  enabled?: boolean;
  silence_threshold_minutes: number;
  check_interval_minutes: number;
  initiative_probability: number;
  max_daily_initiatives: number;
  adaptive_threshold: boolean;
  feedback_enabled: boolean;
  initiative_hours?: string | null; // окно самоинициативы "HH:MM-HH:MM"; null — круглые сутки
}

export interface InitiativeData extends InitiativeParams {
  enabled: boolean;
  ignore_streak: number;
  // Порог молчания, после которого персона пишет сама (адаптивный или
  // заданный), минуты; нет у старого сервера
  effective_silence_minutes?: number;
  initiatives_today: number;
  emotional_state: string;
  history: { message: string; timestamp: number; date: string; type: string }[];
}

// Живая персона (GET /state): состояние + мир + лента последних событий
export interface LivingMood {
  valence: number;
  arousal: number;
  tag: string;
  // направление последнего сдвига valence (старый бэкенд — поля нет)
  trend?: 'up' | 'down' | 'flat';
}

export interface LivingPersonaState {
  energy: number;
  mood: LivingMood;
  pastime: string;
  location: string;
  last_tick_at: number;
  updated_at: string;
  internal_note?: string;
}

export interface LivingStoryline {
  id: number;
  title: string;
  status: string;
  summary: string;
}

export interface LivingFeedEntry {
  id: number;
  timestamp: string;
  type: string;
  payload: { event?: string; content?: string; diff?: Record<string, unknown> };
  consumed?: boolean; // озвучено пользователю (дневник/инициатива) — приглушаем
}

export interface LivingStateData {
  enabled: boolean;
  ui_sync: boolean;
  state: LivingPersonaState | null;
  world?: {
    storylines: LivingStoryline[];
    npcs: { id: number; name: string; role: string }[];
    places: { id: number; name: string; type: string }[];
  };
  last_events?: LivingFeedEntry[];
  // лента комнаты: недавние события независимо от consumed
  recent_events?: LivingFeedEntry[];
}

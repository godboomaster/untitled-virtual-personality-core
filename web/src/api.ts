/* HTTP-клиент бэкенда (app/api/server.py). Базовый URL — из
   VITE_API_URL (см. web/.env), по умолчанию локальный сервер.
   Токен (если на бэке задан API_TOKEN) вводится на экране запуска и
   хранится в localStorage под ключом vpc-api-token. */

import type { RoomArtData, RoomArtPatch, RoomFocus, RoomLayout, RoomLayoutPatch, RoomSource, RoomStyle, RoomView } from './room/roomTypes';

const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? 'http://127.0.0.1:8000';
// Адрес бэкенда без схемы — для подписей в UI (экран загрузки)
export const API_HOST = BASE_URL.replace(/^https?:\/\//, '');

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

// Пользователь веб-интерфейса — один на всех персон (память у персон изолирована контекстом)
export const WEB_USER_ID = 'web_user';
// chat_id веб-чата: фичи ядра (todo/напоминания/обучение/проактивность) ключуются по chat_id,
// при chat_id=null они в process_message не срабатывают
export const WEB_CHAT_ID = 'web_user';

export interface ApiPersona {
  id: string;
  name: string;
  description: string;
  color?: string | null; // цвет метки персоны (общий календарь)
  features: Record<string, unknown>;
  settings: { temperature?: number; max_tokens?: number; top_p?: number };
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
  images?: string[]; // скриншоты страницы (dataURL) из режима управления
  reply_ts?: number | null; // метка ответа в STM (серверные секунды) — место пузыря в ленте
}

// Части «Очистить диалог» (app/api/schemas.py: ClearPart) — в порядке кнопок досье
export const CLEAR_PARTS = [
  'stm', 'ltm', 'diary', 'todo', 'reminders', 'dossier',
  'learning', 'initiatives', 'rhythm', 'living', 'webchat', 'control',
] as const;
export type ClearPart = (typeof CLEAR_PARTS)[number];

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
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
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* тело не JSON — оставляем HTTP-код */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: (init?: RequestInit) => request<{ status: string }>('/api/health', init),

  getPersonas: () => request<ApiPersona[]>('/api/personas'),

  // Создать персону из YAML (имя файла = поле id; 409 — такая уже есть)
  createPersona: (yaml: string) =>
    request<{ ok: boolean; persona: string }>('/api/personas', {
      method: 'POST',
      body: JSON.stringify({ yaml }),
    }),

  deletePersona: (persona: string) =>
    request<{ status: string }>(`/api/personas/${encodeURIComponent(persona)}`, { method: 'DELETE' }),

  // Копия персоны: новый id ({id}_copy) и имя + «(копия)»
  duplicatePersona: (persona: string) =>
    request<{ ok: boolean; persona: string }>(
      `/api/personas/${encodeURIComponent(persona)}/duplicate`,
      { method: 'POST' },
    ),

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

  // «Заглянул в комнату»: до LLM доходит, только если разрешено фичей и лимитом
  pokeRoom: (persona: string, chatId: string = 'auto') =>
    request<{ ok: boolean; delivered: boolean }>(`/api/personas/${encodeURIComponent(persona)}/room/poke`, {
      method: 'POST',
      body: JSON.stringify({ chat_id: chatId }),
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

  getInbox: (persona: string, focused?: boolean) =>
    request<{ messages: InboxMessage[]; generating?: boolean; last_ts?: number; control_mode?: boolean }>(
      `/api/personas/${encodeURIComponent(persona)}/inbox?chat_id=${WEB_CHAT_ID}${focused ? '&focused=1' : ''}`,
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
  renamePersona: (persona: string, newId: string) =>
    request<{ ok: boolean; persona: string; restart_required: boolean }>(`/api/personas/${encodeURIComponent(persona)}/rename`, {
      method: 'POST',
      body: JSON.stringify({ new_id: newId }),
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
}

// Свежая проверка локальной Ollama (GET /api/providers/local/status)
export interface LocalStatus {
  server: boolean; // Ollama отвечает на /api/tags
  url: string;
  model: string; // настроенная модель (OLLAMA_MODEL)
  model_present: boolean; // установлена ли она
  models: string[]; // что установлено
  available: boolean; // server && model_present
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
// серверная метка ответа в STM. Промис резолвится финальным событием done
// с тем же reply (+ extra_messages).
export async function streamChat(
  params: { persona: string; message: string; userId?: string; userName?: string; replyContext?: string; image?: string },
  onToken: (text: string) => void,
  onPartBreak?: () => void,
  onReplyTs?: (ts: number) => void,
): Promise<ApiChatResponse> {
  // Сетевой сбой до ответа (fetch отклонён) — запрос мог и дойти до сервера:
  // считаем обрывом, чат сверится с историей по last_ts
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/api/chat/stream`, {
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
      else if (event.error) throw new ApiError(500, event.error);
      else if (event.done) return event as ApiChatResponse;
    }
  }
  throw new StreamInterruptedError('Стрим оборвался без финального события');
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
  const res = await fetch(`${BASE_URL}/api/skins/generate`, {
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

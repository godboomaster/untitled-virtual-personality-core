/* HTTP-клиент бэкенда (app/api/server.py). Базовый URL — из
   VITE_API_URL (см. web/.env), по умолчанию локальный сервер.
   Токен (если на бэке задан API_TOKEN) хранится в localStorage
   под ключом vpc-api-token. */

const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? 'http://127.0.0.1:8000';

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
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = localStorage.getItem('vpc-api-token');
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
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
  health: () => request<{ status: string }>('/api/health'),

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

  clearChat: (persona: string, userId: string = WEB_USER_ID) =>
    request<{ status: string }>('/api/chat/clear', {
      method: 'POST',
      // Полный сброс: STM диалога + LTM-факты + дневник персоны.
      // Перед сбросом бэкенд пишет снапшот в корзину (7 дней) — см. restoreClearBackup
      body: JSON.stringify({ persona, user_id: userId, chat_id: WEB_CHAT_ID }),
    }),

  // Корзина очистки: снапшот последнего полного сброса
  getClearBackup: (persona: string) =>
    request<{ exists: boolean; ts?: number; counts?: { stm: number; ltm: number; diary: boolean } }>(
      `/api/personas/${encodeURIComponent(persona)}/clear-backup`,
    ),

  restoreClearBackup: (persona: string) =>
    request<{ status: string; restored: { stm: number; ltm: number; diary: boolean } }>(
      `/api/personas/${encodeURIComponent(persona)}/clear-backup/restore`,
      { method: 'POST', body: JSON.stringify({}) },
    ),

  // Поштучное удаление из STM: index — позиция в списке getHistory
  deleteStmMessage: (persona: string, index: number) =>
    request<{ status: string }>('/api/chat/history/delete', {
      method: 'POST',
      body: JSON.stringify({ persona, index, user_id: WEB_USER_ID, chat_id: WEB_CHAT_ID }),
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

  addReminder: (persona: string, task: string, delaySeconds: number) =>
    request<{ items: ReminderEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/reminders`, {
      method: 'POST',
      body: JSON.stringify({ task, delay_seconds: delaySeconds, chat_id: WEB_CHAT_ID }),
    }),

  cancelReminder: (persona: string, index: number) =>
    request<{ items: ReminderEntry[] }>(
      `/api/personas/${encodeURIComponent(persona)}/reminders?index=${index}&chat_id=${WEB_CHAT_ID}`,
      { method: 'DELETE' },
    ),

  getInventory: (persona: string) =>
    request<{ items: InventoryEntry[] }>(`/api/personas/${encodeURIComponent(persona)}/inventory`),

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
  // ПО ЭТОМУ чату: persona + chat_id, иначе одна вкладка морозила фон всех
  // персон и всех чатов (см. app/core/presence.py)
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
    local_tasks: LocalTaskInfo[]; // движки задач локального движка (Ollama/веб-чат)
  }>('/api/providers'),

  getLocalStatus: () =>
    request<LocalStatus>('/api/providers/local/status'),

  // Веб-чаты как провайдеры без API-ключей: список сайтов в порядке
  // перебора (deepseek|qwen|claude), [] — выключить
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

  // Движок конкретной локальной задачи (классификаторы, тики жизни и т.п.):
  // 'ollama' | 'webchat'; site — сайт веб-чата для задачи (пусто — первый включённый)
  setLocalTask: (task: string, backend: 'ollama' | 'webchat', site?: string | null) =>
    request<{ ok: boolean; task: string; backend: string }>(`/api/providers/local-tasks/${encodeURIComponent(task)}`, {
      method: 'PUT',
      body: JSON.stringify({ backend, site: site ?? null }),
    }),

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

// Задача локального движка и её текущий движок (выбор пользователя)
export interface LocalTaskInfo {
  id: string;
  backend: 'ollama' | 'webchat';
  site: string | null; // сайт веб-чата (когда backend === 'webchat')
  ollama_only: boolean; // технически не может уйти в веб-чат (OCR)
}

export interface PersonaLlmConfig {
  primary: string | null; // null — глобальный активный провайдер
  fallback: string[]; // приоритет fallback-цепочки (id провайдеров)
  models: Record<string, string>; // свои модели по провайдерам (override глобальной)
  // провайдеры по назначению (null/отсутствует — обычная цепочка):
  answer_provider?: string | null; // текст ответа пользователю
  cc_provider?: string | null; // решения режима управления (разбор/резолв)
  vision_provider?: string | null; // vision-фолбэк (картинки)
  // лимиты веб-чатов: {сайт: {enabled, per_hour}}; сайта нет — дефолт 40/час
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
// следующие токены относятся к новому сообщению. Промис резолвится
// финальным событием done с тем же reply (+ extra_messages).
export async function streamChat(
  params: { persona: string; message: string; userId?: string; userName?: string; replyContext?: string; image?: string },
  onToken: (text: string) => void,
  onPartBreak?: () => void,
): Promise<ApiChatResponse> {
  const token = localStorage.getItem('vpc-api-token');
  const res = await fetch(`${BASE_URL}/api/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
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
  if (!res.ok || !res.body) {
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
    const { done, value } = await reader.read();
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
      else if (event.error) throw new ApiError(500, event.error);
      else if (event.done) return event as ApiChatResponse;
    }
  }
  throw new ApiError(500, 'Стрим оборвался без финального события');
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
  task: string;
  trigger_at: number | null;
  recurrence: { type: 'daily' | 'weekly'; hour: number; minute: number; weekday?: number | null } | null;
  user_name: string;
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
  initiatives_today: number;
  emotional_state: string;
  history: { message: string; timestamp: number; date: string; type: string }[];
}

// Живая персона (GET /state): состояние + мир + лента последних событий
export interface LivingMood {
  valence: number;
  arousal: number;
  tag: string;
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

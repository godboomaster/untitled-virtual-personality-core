/* Доступ к бэкенду из UI: общий (на всё приложение) одноразовый фетч
   списка персон. Если бэкенд недоступен — возвращаем null, и секции
   работают на моковых данных (прототипный режим). */

import { useEffect, useReducer } from 'react';
import { api, ApiError, isNativeApp } from './api';
import type { ApiPersona, LivingStateData, PersonaLlmConfig, ProviderInfo } from './api';
import type { Persona, PersonaFeature } from './mockData';
import { saveProvidersCopy } from './offline/settingsCopy';
import { localApiPersona, localPersonas, onLocalPersonasChange } from './offline/localPersonas';
import type { LocalApiPersona } from './offline/localPersonas';

// Коды фич, известные UI (подписи — fb.* в словарях i18n)
const KNOWN_FEATURES = new Set<PersonaFeature>([
  'LTM', 'RAG', 'proactive', 'reminder', 'web_search', 'learning',
  'self_memory', 'todo', 'admin', 'group_mode', 'inventory',
  'rate_limit', 'moderation', 'punish_block', 'file_upload', 'light_context',
]);

// YAML-флаги персоны (dict) → коды фич UI
function mapFeatures(features: Record<string, unknown>): PersonaFeature[] {
  const out: PersonaFeature[] = ['LTM']; // память есть у всех персон ядра
  for (const [key, value] of Object.entries(features)) {
    const on = value === true || (key === 'proactive' && (value as { enabled?: boolean })?.enabled === true);
    if (!on) continue;
    // book_search — это и есть RAG по книге
    const code = key === 'book_search' ? 'RAG' : key;
    if (KNOWN_FEATURES.has(code as PersonaFeature) && !out.includes(code as PersonaFeature)) {
      out.push(code as PersonaFeature);
    }
  }
  return out;
}

function mapPersona(p: ApiPersona | LocalApiPersona): Persona {
  const muted = p.features?.muted === true;
  return {
    ...('local' in p ? { local: p.local } : {}),
    id: p.id,
    name: p.name,
    description: p.description,
    model: '—', // активный провайдер API пока не отдаёт
    color: p.color ?? null,
    features: mapFeatures(p.features),
    lastReply: '—',
    lastReplyFreshness: 'fresh',
    status: muted ? 'frozen' : 'online',
    muted,
    builtin: p.builtin ?? false,
    customized: p.customized ?? false,
    temperature: p.settings.temperature ?? 0.7,
    maxTokens: p.settings.max_tokens ?? 2000,
    topP: p.settings.top_p ?? 0.9,
  };
}

// Общий кеш: фетч один раз, все потребители делят результат. После неудачи
// кеш НЕ защёлкивается навсегда: бэкенд мог просто перезапускаться — при
// следующем монтировании потребителя (навигация по секциям) пробуем снова.
let cache: Persona[] | null = null;
let failedAt = 0; // время последней неудачной попытки; 0 — не было

// Приложение на телефоне: снимок списка персон — чтобы без ноутбука
// (web/src/offline) открыть чаты по сохранённым данным
const PERSONAS_SNAPSHOT_KEY = 'vpc-offline-personas';

function saveSnapshot(list: ApiPersona[]) {
  if (!isNativeApp()) return;
  try {
    localStorage.setItem(PERSONAS_SNAPSHOT_KEY, JSON.stringify(list));
  } catch {
    /* хранилище недоступно — без офлайн-запуска */
  }
}

function readSnapshot(): ApiPersona[] {
  try {
    const list = JSON.parse(localStorage.getItem(PERSONAS_SNAPSHOT_KEY) || '[]') as ApiPersona[];
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

/** Запуск без ноутбука: список персон из снимка. false — снимка нет. */
export function seedOfflinePersonas(): boolean {
  const list = readSnapshot();
  if (!list.length && !localPersonas().length) return false;
  cache = list.map(mapPersona);
  listeners.forEach((l) => l());
  return true;
}

/** Персоны из снимка и созданные на телефоне (id, имя, описание) — для
 * ответов без ядра. */
export function offlinePersonaList(): { id: string; name: string; description: string }[] {
  const out = readSnapshot().map((p) => ({ id: p.id, name: p.name, description: p.description || '' }));
  const have = new Set(out.map((p) => p.id));
  for (const p of localPersonas()) if (!have.has(p.id)) out.push({ id: p.id, name: p.name, description: p.description });
  return out;
}

export function hasOfflinePersonas(): boolean {
  try {
    return !!localStorage.getItem(PERSONAS_SNAPSHOT_KEY) || localPersonas().length > 0;
  } catch {
    return false;
  }
}

/** Персона только что появилась на ядре (перенос с телефона) — в список и
 * снимок сразу, не дожидаясь перечитки: иначе на время запроса она пропала
 * бы из списков, и открытый с ней чат переключился бы на другую. */
export function upsertCachedPersona(p: ApiPersona) {
  if (cache === null) return;
  cache = [...cache.filter((x) => x.id !== p.id), mapPersona(p)];
  saveSnapshot([...readSnapshot().filter((x) => x.id !== p.id), p]);
  listeners.forEach((l) => l());
}

// Персоны, созданные на телефоне без ядра (offline/localPersonas.ts), — в
// конце списка, пока их нет на ядре. Тот же массив, пока не сменились ни
// кеш, ни они: потребители держат список в зависимостях эффектов
let mergedBase: Persona[] | null = null;
let mergedLocal: unknown = null;
let merged: Persona[] | null = null;

function withLocal(): Persona[] | null {
  if (cache === null) return null;
  const local = localPersonas();
  if (cache === mergedBase && local === mergedLocal) return merged;
  const have = new Set(cache.map((p) => p.id));
  const extra = local.filter((p) => !have.has(p.id)).map((p) => mapPersona(localApiPersona(p)));
  mergedBase = cache;
  mergedLocal = local;
  merged = extra.length ? [...cache, ...extra] : cache;
  return merged;
}
let inflight: Promise<void> | null = null;
const listeners = new Set<() => void>();
onLocalPersonasChange(() => listeners.forEach((l) => l()));
const RETRY_AFTER_MS = 5000; // антифлап-пауза между попытками после неудачи

function ensureFetch(): Promise<void> {
  if (inflight) return inflight;
  if (cache !== null) return Promise.resolve();
  if (failedAt && Date.now() - failedAt < RETRY_AFTER_MS) return Promise.resolve();
  inflight = api
    .getPersonas()
    .then((list) => {
      cache = list.map(mapPersona);
      failedAt = 0;
      saveSnapshot(list);
    })
    .catch(() => {
      failedAt = Date.now(); // бэкенд недоступен — моковый режим до следующей попытки
    })
    .finally(() => {
      inflight = null;
      listeners.forEach((l) => l());
    });
  return inflight;
}

// Персоны с бэкенда; null — бэкенд недоступен (или ещё грузится)
export function useApiPersonas(): Persona[] | null {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    ensureFetch();
    return () => {
      listeners.delete(force);
    };
  }, []);
  return withLocal();
}

// Сбросить кеш и перечитать список (после создания/удаления/дублирования персоны).
// Промис резолвится, когда новый список уже в кеше (смена id ждёт его,
// чтобы переключить выбор на новый id, а не на первую персону).
// Ошибка запроса — в результате (null — список получен): экран запуска
// по 401 спрашивает токен
export function refetchPersonas(): Promise<ApiError | null> {
  // Старый список держим до прихода нового: сброс кеша в null на время запроса
  // выглядел бы как «бэкенд офлайн» (мок-данные, сброс выбора персоны в чате)
  failedAt = 0;
  return api
    .getPersonas()
    .then((list) => {
      cache = list.map(mapPersona);
      saveSnapshot(list);
      return null;
    })
    .catch((e: unknown) => {
      // Телефон без связи с ядром: прежний список остаётся (без ядра он и так
      // из снимка) — иначе приложение ушло бы в демо-данные посреди работы
      // без ядра (перечитка после переноса персоны с телефона, offline/personaSync.ts)
      if (!(isNativeApp() && cache !== null && !(e instanceof ApiError))) cache = null;
      failedAt = Date.now();
      return e instanceof ApiError ? e : new ApiError(0, String(e));
    })
    .finally(() => listeners.forEach((l) => l()));
}

// true — бэкенд отвечает, можно ходить в API за чатом/памятью
export function useApiOnline(): boolean {
  return useApiPersonas() !== null;
}

// ── Связь с ядром (шапка, подвал сайдбара) ───────────────────────────
// Список персон грузится один раз, и падение ядра посреди работы без
// отдельного опроса было не заметно: шапка писала «все системы в норме».
// /api/health — раз в 15 с, пока вкладка видна; ответ дольше 5 с — нет связи.
// «Нет связи» — только после двух неудач подряд (вторая проверка через 3 с):
// ядро бывает занято секунды (создание персоны при первом открытии чата), а
// на телефоне «нет связи» включает ответы без ноутбука (web/src/offline)

export type CoreHealth = 'ok' | 'down' | 'unknown';

const HEALTH_POLL_MS = 15_000;
const HEALTH_TIMEOUT_MS = 5_000;
const HEALTH_RECHECK_MS = 3_000;
let health: CoreHealth = 'unknown';
let healthTimer: number | null = null;
let healthFails = 0;
const healthListeners = new Set<() => void>();

function setHealth(next: CoreHealth) {
  if (next === health) return;
  health = next;
  healthListeners.forEach((fn) => fn());
}

function checkHealth() {
  if (document.hidden) return;
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), HEALTH_TIMEOUT_MS);
  api
    .health({ signal: ctl.signal })
    .then(() => {
      healthFails = 0;
      setHealth('ok');
    })
    .catch(() => {
      healthFails += 1;
      if (healthFails >= 2 || health === 'down') setHealth('down');
      else window.setTimeout(checkHealth, HEALTH_RECHECK_MS);
    })
    .finally(() => window.clearTimeout(timer));
}

const onHealthVisibility = () => {
  if (!document.hidden) checkHealth();
};

/** Связь с ядром сейчас — для фоновых задач вне React (offline/personaSync.ts). */
export function coreHealth(): CoreHealth {
  return health;
}

export function useCoreHealth(): CoreHealth {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    healthListeners.add(force);
    if (healthTimer == null) {
      checkHealth();
      healthTimer = window.setInterval(checkHealth, HEALTH_POLL_MS);
      document.addEventListener('visibilitychange', onHealthVisibility);
    }
    return () => {
      healthListeners.delete(force);
      if (!healthListeners.size && healthTimer != null) {
        window.clearInterval(healthTimer);
        healthTimer = null;
        document.removeEventListener('visibilitychange', onHealthVisibility);
      }
    };
  }, []);
  return health;
}

// ── Провайдеры LLM с бэкенда (общий кеш, как у персон) ───────────────

let provCache: ProviderInfo[] | null = null;
let provWebchat: { sites: string[]; options: string[] } | null = null;
let provFailed = false;
let provInflight = false;
const provListeners = new Set<() => void>();

function ensureProvidersFetch() {
  if (provCache !== null || provFailed || provInflight) return;
  provInflight = true;
  api
    .getProviders()
    .then((r) => {
      provCache = r.providers;
      provWebchat = {
        sites: r.webchat_sites ?? (r.webchat_site ? [r.webchat_site] : []),
        options: r.webchat_options ?? [],
      };
      // Телефон: копия для досье без связи с ядром
      saveProvidersCopy(provCache, provWebchat);
    })
    .catch(() => {
      provFailed = true;
    })
    .finally(() => {
      provInflight = false;
      provListeners.forEach((l) => l());
    });
}

// Сбросить кеш и перечитать (после добавления ключа/смены активного)
export function refetchProviders() {
  provCache = null;
  provFailed = false;
  provInflight = false;
  ensureProvidersFetch();
  provListeners.forEach((l) => l());
}

function useProvListener() {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    provListeners.add(force);
    ensureProvidersFetch();
    return () => {
      provListeners.delete(force);
    };
  }, []);
}

// Провайдеры с бэкенда; null — бэкенд недоступен (или ещё грузится)
export function useApiProviders(): ProviderInfo[] | null {
  useProvListener();
  return provCache;
}

// Веб-чаты с бэкенда: включённые сайты (порядок перебора) и доступные;
// null — бэкенд недоступен (или ещё грузится)
export function useApiWebchat(): { sites: string[]; options: string[] } | null {
  useProvListener();
  return provWebchat;
}

// ── Персональный LLM-конфиг (primary + свои модели), по id персоны ────
// Шапка чата показывает его как «кто ответит сейчас»; досье инвалидирует
// кеш после сохранения — шапка обновляется сразу, не дожидаясь ответа.

const llmCache: Record<string, PersonaLlmConfig> = {};
const llmInflight = new Set<string>();
const llmListeners = new Set<() => void>();

function ensurePersonaLlmFetch(persona: string) {
  if (llmCache[persona] || llmInflight.has(persona)) return;
  llmInflight.add(persona);
  api
    .getPersonaConfig(persona)
    .then((c) => {
      llmCache[persona] = c.llm;
    })
    .catch(() => {})
    .finally(() => {
      llmInflight.delete(persona);
      llmListeners.forEach((l) => l());
    });
}

// Перечитать llm-конфиг персоны (вызывать после сохранения в досье)
export function refetchPersonaLlm(persona: string) {
  delete llmCache[persona];
  ensurePersonaLlmFetch(persona);
}

// llm-конфиг персоны с бэкенда; null — ещё не загружен
export function useApiPersonaLlm(persona: string): PersonaLlmConfig | null {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    llmListeners.add(force);
    ensurePersonaLlmFetch(persona);
    return () => {
      llmListeners.delete(force);
    };
  }, [persona]);
  return llmCache[persona] ?? null;
}

// ── Живое состояние персоны (слой state/world): опрос раз в минуту ────
// Данные для вкладок комната/настроение (ui_room_mood_sync). Пока запрос
// не удался — null, и Room работает на моковых телеметрических значениях.

const livingCache: Record<string, LivingStateData> = {};
const livingInflight = new Set<string>();
const livingListeners = new Set<() => void>();
const LIVING_POLL_MS = 60_000;

function ensureLivingFetch(persona: string) {
  if (livingInflight.has(persona)) return;
  livingInflight.add(persona);
  api
    .getLivingState(persona)
    .then((s) => {
      livingCache[persona] = s;
    })
    .catch(() => {
      // Бэкенд без living-фичи или недоступен — оставляем прошлый кеш
    })
    .finally(() => {
      livingInflight.delete(persona);
      livingListeners.forEach((l) => l());
    });
}

// Живое состояние персоны; null — бэкенд недоступен, фича выключена
// или ui_room_mood_sync=false (тогда комната/настроение показывают моки)
export function usePersonaLivingState(persona: string): LivingStateData | null {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    const tick = () => {
      livingListeners.add(force);
      ensureLivingFetch(persona);
    };
    tick();
    const timer = setInterval(() => ensureLivingFetch(persona), LIVING_POLL_MS);
    return () => {
      livingListeners.delete(force);
      clearInterval(timer);
    };
  }, [persona]);
  const cached = livingCache[persona];
  if (!cached || !cached.enabled || !cached.ui_sync || !cached.state) return null;
  return cached;
}

import { api, isNativeApp } from '../api';
import type { PersonaConfig, ProviderInfo } from '../api';

/* Копия настроек персоны на телефоне: без связи с ядром досье показывает её
   (только посмотреть), а не демо-данные прототипа — те выглядели как
   настоящие провайдеры персоны. Пишется, когда досье загрузило настройки с
   ядра и при обновлении данных персоны (refreshKit, не чаще COPY_TTL_MS).
   Ключей провайдеров в копии нет — только сколько их. */

const CONFIG_PREFIX = 'vpc-offline-config:';
const PROVIDERS_KEY = 'vpc-offline-providers';
export const COPY_TTL_MS = 10 * 60_000;

type Webchat = { sites: string[]; options: string[] };

export interface SettingsCopy {
  config: PersonaConfig;
  providers: ProviderInfo[];
  webchat: Webchat;
  at: number; // мс: копия от (старшая из двух частей)
}

function write(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* хранилище недоступно — без копии */
  }
}

function read<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

/** Провайдеры ядра (общие для всех персон) — в копию. */
export function saveProvidersCopy(providers: ProviderInfo[], webchat: Webchat) {
  if (!isNativeApp()) return;
  write(PROVIDERS_KEY, { providers: providers.map((p) => ({ ...p, keys: [] })), webchat, at: Date.now() });
}

/** Настройки персоны (YAML: генерация, фичи, llm) — в копию. */
export function savePersonaConfigCopy(persona: string, config: PersonaConfig) {
  if (!isNativeApp()) return;
  write(CONFIG_PREFIX + persona, { config, at: Date.now() });
}

/** Копия для досье без связи; null — нет настроек персоны или провайдеров. */
export function getSettingsCopy(persona: string): SettingsCopy | null {
  const c = read<{ config: PersonaConfig; at: number }>(CONFIG_PREFIX + persona);
  const p = read<{ providers: ProviderInfo[]; webchat: Webchat; at: number }>(PROVIDERS_KEY);
  if (!c?.config || !p?.providers) return null;
  return { config: c.config, providers: p.providers, webchat: p.webchat ?? { sites: [], options: [] }, at: Math.min(c.at, p.at) };
}

/** Освежить копию с ядра: настройки персоны и, если устарели, провайдеры. */
export async function refreshSettingsCopy(persona: string): Promise<void> {
  if (!isNativeApp()) return;
  savePersonaConfigCopy(persona, await api.getPersonaConfig(persona));
  const have = read<{ at: number }>(PROVIDERS_KEY);
  if (have && Date.now() - have.at < COPY_TTL_MS) return;
  const r = await api.getProviders();
  saveProvidersCopy(r.providers, {
    sites: r.webchat_sites ?? (r.webchat_site ? [r.webchat_site] : []),
    options: r.webchat_options ?? [],
  });
}

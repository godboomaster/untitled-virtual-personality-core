import { api, ApiError } from '../api';
import { offlinePersonaList } from '../apiData';
import { OfflineLlm } from './plugin';
import type { PhoneProvider, RingProvider } from './plugin';

/* Ключи облачных моделей, введённые на самом телефоне, — для приложения без
   компьютера («Настройки» → «Ключи на телефоне», PhoneKeysCard). Хранит их
   нативный плагин (зашифрованы ключом Android Keystore), вебу — только маски.
   Общие для всех персон: без компьютера ими отвечает любая; с компьютером —
   после ключей персоны из ядра (ядро главное).

   Компьютер появился — телефон один раз спрашивает, перенести ли ключи туда
   (KeysMigrateDialog). Перенос только добавляет: ключи — к ключам провайдера
   в .env ядра (такой же ядро не возьмёт второй раз), модель — если у
   провайдера на ядре её нет, основной — если на ядре он не выбран. На
   телефоне ключи остаются. Ответ запоминается по набору ключей: новые
   ключи — спросит снова. */

// Провайдеры ядра (app/core/config.py PROVIDER_CONFIGS): адрес — по
// умолчанию ядра, модель — только пример в поле (по умолчанию её нет и у ядра)
export const PHONE_CATALOG: { id: string; name: string; base_url: string; example: string; temperature?: number }[] = [
  { id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1', example: 'gpt-4o-mini' },
  { id: 'google', name: 'Google', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', example: 'gemini-2.5-flash' },
  { id: 'anthropic', name: 'Anthropic', base_url: 'https://api.anthropic.com/v1', example: 'claude-haiku-4-5' },
  { id: 'deepseek', name: 'DeepSeek', base_url: 'https://api.deepseek.com', example: 'deepseek-chat' },
  { id: 'groq', name: 'Groq', base_url: 'https://api.groq.com/openai/v1', example: 'llama-3.3-70b-versatile' },
  { id: 'zai', name: 'ZAI', base_url: 'https://open.bigmodel.cn/api/paas/v4', example: 'glm-4.5-flash' },
  // Kimi принимает только temperature 1 (как KIMI_TEMPERATURE ядра)
  { id: 'kimi', name: 'Kimi', base_url: 'https://api.moonshot.cn/v1', example: 'kimi-k2-0905-preview', temperature: 1 },
  { id: 'mimo', name: 'Mimo', base_url: 'https://token-plan-sgp.xiaomimimo.com/v1', example: '' },
  { id: 'hf', name: 'HuggingFace', base_url: 'https://router.huggingface.co/v1', example: 'Qwen/Qwen2.5-72B-Instruct' },
];

const listeners = new Set<() => void>();

export function onPhoneKeysChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export async function phoneProviders(): Promise<PhoneProvider[]> {
  return (await OfflineLlm.phoneProviders()).providers;
}

type Update = { model?: string; addKey?: string; removeKey?: number; remove?: boolean; top?: boolean };

/** Правка провайдера телефона; адрес и своя температура — из каталога
 * (провайдера нет в каталоге — адрес baseUrl, с ядра). */
export async function updatePhoneProvider(id: string, change: Update, baseUrl?: string): Promise<PhoneProvider[]> {
  const cat = PHONE_CATALOG.find((p) => p.id === id);
  const url = cat?.base_url ?? baseUrl;
  const r = await OfflineLlm.updatePhoneProvider({
    name: id,
    ...(url ? { base_url: url } : {}),
    ...(cat?.temperature != null ? { temperature: cat.temperature } : {}),
    ...change,
  });
  listeners.forEach((l) => l());
  return r.providers;
}

/** Все ключи телефона по провайдерам: с ядра (по порядку персон) и свои. */
export async function keyring(): Promise<RingProvider[]> {
  return (await OfflineLlm.keyring({ personas: offlinePersonaList().map((p) => p.id) })).providers;
}

/** Правка ключей ядра на телефоне (своя модель, убрать/вернуть ключ, снять правки). */
export async function editProvider(opts: { name: string; model?: string; hideKey?: string; showKey?: string; reset?: boolean }) {
  await OfflineLlm.editProvider(opts);
  listeners.forEach((l) => l());
}

// ── Перенос на компьютер ──────────────────────────────────────────────

const OFFER_KEY = 'vpc-phone-keys-offer';

/** Набор ключей телефона (маски) — спрашивали ли уже о нём. */
function signature(list: PhoneProvider[]): string {
  return list
    .map((p) => `${p.name}:${p.keys.join(',')}`)
    .sort()
    .join('|');
}

/** Ключи на телефоне, о переносе которых ещё не спрашивали; [] — спрашивать не о чем. */
export async function keysToOffer(): Promise<PhoneProvider[]> {
  const list = (await phoneProviders().catch(() => [] as PhoneProvider[])).filter((p) => p.keys.length);
  if (!list.length) return [];
  try {
    if (localStorage.getItem(OFFER_KEY) === signature(list)) return [];
  } catch {
    /* хранилище недоступно — спросим */
  }
  return list;
}

/** Ответ на вопрос о переносе — этот набор ключей больше не предлагать. */
export function rememberOffer(list: PhoneProvider[]) {
  try {
    localStorage.setItem(OFFER_KEY, signature(list));
  } catch {
    /* хранилище недоступно — спросит ещё раз */
  }
}

export interface KeysMoved {
  keys: number; // добавлено ключей на ядро
  models: string[]; // провайдеры, которым задана модель с телефона
  active: string | null; // основной на ядре стал этот (не был выбран)
}

/** Перенести ключи телефона на ядро — только добавить. Сеть оборвалась — наружу. */
export async function moveKeysToCore(): Promise<KeysMoved> {
  const { providers } = await OfflineLlm.exportPhoneProviders();
  const core = await api.getProviders();
  const byId = new Map(core.providers.map((p) => [p.id, p]));
  const out: KeysMoved = { keys: 0, models: [], active: null };
  for (const p of providers) {
    const info = byId.get(p.name);
    if (!info || info.local) continue; // провайдера на ядре нет — некуда
    for (const key of p.api_keys) {
      try {
        if ((await api.addProviderKey(p.name, key)).ok) out.keys++;
      } catch (e) {
        if (!(e instanceof ApiError)) throw e; // 400 — такой ключ на ядре уже есть
      }
    }
    if (p.model && !info.model) {
      try {
        await api.setProviderModel(p.name, p.model);
        out.models.push(p.name);
      } catch (e) {
        if (!(e instanceof ApiError)) throw e;
      }
    }
  }
  // Основной на ядре не выбран — первый с телефона (у него есть и ключ, и модель)
  const first = providers.find((p) => p.api_keys.length && (p.model || byId.get(p.name)?.model));
  if (!core.active && first && byId.has(first.name)) {
    try {
      await api.setActiveProvider(first.name);
      out.active = first.name;
    } catch (e) {
      if (!(e instanceof ApiError)) throw e;
    }
  }
  return out;
}

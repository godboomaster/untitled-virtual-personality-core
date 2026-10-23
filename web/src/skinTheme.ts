import { useEffect, useSyncExternalStore } from 'react';
import { getSkin, loadSkinCss } from '../skins';
import { getInitialTheme } from './useAppTheme';
import type { AppTheme } from './useAppTheme';

/* Выбор темы оформления (скина из web/skins/): на всё приложение и своя
   у отдельных персон. Своя тема персоны действует, пока открыт её экран —
   чат, досье, комната (usePersonaSkinScope); на остальных экранах — общая.
   Выбор живёт в localStorage этого устройства: на компьютере и в
   приложении на телефоне он свой.

   Схема (светлая/тёмная): выбор в шапке — пожелание; у скина, который
   умеет только одну схему, действует его схема. Атрибуты на <html> —
   data-skin и data-theme — ставит один хозяин, SkinHost, и только когда CSS
   скина уже загружен: без промежуточного кадра «новая схема, старый вид». */

const GLOBAL_KEY = 'vpc-skin';
const PERSONAS_KEY = 'vpc-skin-personas';
// Значение в карте персон: у этой персоны обычный вид, даже если общая тема задана
export const PERSONA_DEFAULT = 'default';

interface SkinThemeState {
  global: string | null;
  personas: Record<string, string>;
  scope: string | null; // персона, чей экран сейчас открыт
  preference: AppTheme; // выбор в шапке
  applied: string | null; // скин, который уже стоит на <html>
}

function readGlobal(): string | null {
  try {
    const id = localStorage.getItem(GLOBAL_KEY);
    return getSkin(id) ? id : null;
  } catch {
    return null;
  }
}

function readPersonas(): Record<string, string> {
  try {
    const raw = JSON.parse(localStorage.getItem(PERSONAS_KEY) ?? '{}') as unknown;
    if (!raw || typeof raw !== 'object') return {};
    const out: Record<string, string> = {};
    for (const [pid, id] of Object.entries(raw as Record<string, unknown>)) {
      if (typeof id === 'string' && (id === PERSONA_DEFAULT || getSkin(id))) out[pid] = id;
    }
    return out;
  } catch {
    return {};
  }
}

let state: SkinThemeState = {
  global: readGlobal(),
  personas: readPersonas(),
  scope: null,
  preference: getInitialTheme(),
  applied: null,
};
const listeners = new Set<() => void>();

function update(patch: Partial<SkinThemeState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useSkinThemeState(): SkinThemeState {
  return useSyncExternalStore(subscribe, () => state);
}

function save(key: string, value: string | null) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* приватный режим или полное хранилище — выбор живёт до перезагрузки */
  }
}

export function setGlobalSkin(id: string | null) {
  const next = getSkin(id) ? id : null;
  save(GLOBAL_KEY, next);
  update({ global: next });
}

// id — скин, PERSONA_DEFAULT (обычный вид) или null (как у всего приложения)
export function setPersonaSkin(personaId: string, id: string | null) {
  const personas = { ...state.personas };
  if (id === PERSONA_DEFAULT || getSkin(id)) personas[personaId] = id as string;
  else delete personas[personaId];
  save(PERSONAS_KEY, Object.keys(personas).length ? JSON.stringify(personas) : null);
  update({ personas });
}

// Смена id персоны: своя тема переезжает вместе с ней
export function renamePersonaSkinTheme(oldId: string, newId: string) {
  const id = state.personas[oldId];
  if (id === undefined) return;
  setPersonaSkin(oldId, null);
  setPersonaSkin(newId, id);
}

export function setThemePreference(preference: AppTheme) {
  if (state.preference !== preference) update({ preference });
}

export function setAppliedSkin(id: string | null) {
  if (state.applied !== id) update({ applied: id });
}

// Скин, который должен стоять сейчас (с учётом открытого экрана персоны)
export function effectiveSkin(s: SkinThemeState = state): string | null {
  const own = s.scope ? s.personas[s.scope] : undefined;
  if (own === PERSONA_DEFAULT) return null;
  if (own && getSkin(own)) return own;
  return s.global;
}

// Схема, которая действует при скине: выбранная, если скин её умеет
export function resolveScheme(preference: AppTheme, skinId: string | null): AppTheme {
  const skin = getSkin(skinId);
  if (!skin || skin.schemes.length === 0 || skin.schemes.includes(preference)) return preference;
  return skin.schemes[0];
}

export function useEffectiveSkin(): { skinId: string | null; scheme: AppTheme } {
  const s = useSkinThemeState();
  const skinId = effectiveSkin(s);
  return { skinId, scheme: resolveScheme(s.preference, skinId) };
}

// Скин уже на <html> (CSS загружен): по нему прячется стандартный фон
export function useAppliedSkin(): string | null {
  return useSkinThemeState().applied;
}

// Экран персоны: пока смонтирован, действует её своя тема (если задана)
export function usePersonaSkinScope(personaId: string | null | undefined) {
  useEffect(() => {
    if (!personaId) return;
    update({ scope: personaId });
    return () => {
      if (state.scope === personaId) update({ scope: null });
    };
  }, [personaId]);
}

// До первого рендера: схема и общая тема — сразу, чтобы экран запуска
// не мелькнул стандартным видом. Ждём CSS не дольше maxWaitMs.
export function initSkinTheme(maxWaitMs = 1500): Promise<void> {
  const root = document.documentElement;
  const skinId = effectiveSkin();
  root.setAttribute('data-theme', resolveScheme(state.preference, skinId));
  if (!skinId) return Promise.resolve();
  const loaded = loadSkinCss(skinId).then(() => {
    if (effectiveSkin() !== skinId || state.applied) return; // SkinHost уже успел своё
    root.setAttribute('data-skin', skinId);
    setAppliedSkin(skinId);
  });
  return Promise.race([loaded, new Promise<void>((r) => setTimeout(r, maxWaitMs))]);
}

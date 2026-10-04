// Стор пользовательского арта для персон: спрайт (+ спрайты по позам), фон
// комнаты и стиль (эталонный референс + текстовое описание). Простой
// module-store на useSyncExternalStore — Art и Room видят одни данные.
// Персистентность — localStorage (dataURL), с защитой от переполнения квоты.
// Сервер (см. room_spec.md: GET/PUT /room/art, /room/style) — источник
// истины при первой синхронизации персоны; офлайн — работаем только с кешем,
// правки уходят на бэкенд с задержкой (дебаунс), как только он доступен.
import { useEffect, useSyncExternalStore } from 'react';
import { useApiOnline } from './apiData';
import { api } from './api';
import type { RoomArtData, RoomArtPatch } from './room/roomTypes';

// Точка в долях кадра (0..1)
export interface AnchorPoint {
  x: number;
  y: number;
}

export interface SpriteAsset {
  dataUrl: string;
  anchor: AnchorPoint; // маркер «ступни» — точка контакта с полом
}

// Ключи точек пола: desk/window/shelf — как и раньше, обязательные (есть у
// любой сцены), bed/chair/floor — опциональные, добавляются/убираются в калибровке
export type FloorSpotKey = 'desk' | 'window' | 'shelf' | 'bed' | 'chair' | 'floor';
export const REQUIRED_FLOOR_KEYS = ['desk', 'window', 'shelf'] as const;
export const OPTIONAL_FLOOR_KEYS = ['bed', 'chair', 'floor'] as const;

// Ключи точек пола совпадают с точками перемещения аватара в комнате
export interface RoomBgAsset {
  dataUrl: string;
  floorPoints: Record<'desk' | 'window' | 'shelf', AnchorPoint> &
    Partial<Record<'bed' | 'chair' | 'floor', AnchorPoint>>;
}

// Позы, которые умеет отдавать бэкенд (см. room_spec.md); у каждой — свой
// промпт и слот загрузки в мастерской, недостающие используют базовый sprite
export type PoseKey = 'stand' | 'sit' | 'read' | 'write' | 'look' | 'sleep';
export const POSE_KEYS: readonly PoseKey[] = ['stand', 'sit', 'read', 'write', 'look', 'sleep'];

export interface PersonaArt {
  sprite?: SpriteAsset;
  sprites?: Partial<Record<PoseKey, SpriteAsset>>;
  roomBg?: RoomBgAsset;
  reference?: string; // dataUrl эталонного референса стиля
  styleDescription?: string; // описание стиля — вручную или из «Определить стиль»
}

// Стиль персонажа отдельным срезом того же стора (описание + референс) —
// используется мастерской и попадает в промпт-паки
export interface PersonaStyle {
  description: string;
  reference?: string;
}

type ArtState = Record<string, PersonaArt>;

const STORAGE_KEY = 'vpc-art';

const listeners = new Set<() => void>();

function load(): ArtState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ArtState) : {};
  } catch {
    return {};
  }
}

let state: ArtState = load();

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Квота localStorage переполнена — арт живёт только в рамках сессии
  }
}

// ── Синхронизация с бэкендом ──────────────────────────────────────────
// online обновляется эффектом usePersonaArt (общий useApiOnline-кеш из
// apiData.ts) — используется отложенными PUT, чтобы не пытаться стучаться
// в сеть, пока бэкенд точно недоступен (мок-режим)
let online = false;
const fetchedPersonas = new Set<string>();
const inflightPersonas = new Set<string>();

const ART_PUT_DEBOUNCE_MS = 1200;
const STYLE_PUT_DEBOUNCE_MS = 1200;

const pendingArtPatch: Record<string, RoomArtPatch> = {};
const artPutTimers: Record<string, ReturnType<typeof setTimeout>> = {};
const pendingStylePatch: Record<string, { description?: string; reference?: string | null }> = {};
const stylePutTimers: Record<string, ReturnType<typeof setTimeout>> = {};

// Маппинг store → art.json бэкенда (room_bg вместо roomBg, остальное совпадает).
// sprites на сервере сливаются по позам: убранная локально поза уходит явным
// null (иначе её спрайт остался бы на сервере и вернулся при следующей синхронизации)
function toBackendArtPatch(patch: Partial<PersonaArt>, prev?: PersonaArt): RoomArtPatch {
  const out: RoomArtPatch = {};
  if ('sprite' in patch) out.sprite = patch.sprite ?? null;
  if ('sprites' in patch) {
    const sprites: Record<string, SpriteAsset | null> = { ...patch.sprites };
    for (const pose of Object.keys(prev?.sprites ?? {})) {
      if (!(pose in sprites)) sprites[pose] = null;
    }
    out.sprites = sprites;
  }
  if ('roomBg' in patch) out.room_bg = patch.roomBg ?? null;
  return out;
}

function fromBackendArt(data: RoomArtData): Partial<PersonaArt> {
  const out: Partial<PersonaArt> = {};
  if (data.sprite) out.sprite = data.sprite;
  if (data.sprites && Object.keys(data.sprites).length) {
    out.sprites = data.sprites as Partial<Record<PoseKey, SpriteAsset>>;
  }
  if (data.room_bg) {
    out.roomBg = { dataUrl: data.room_bg.dataUrl, floorPoints: data.room_bg.floorPoints as RoomBgAsset['floorPoints'] };
  }
  return out;
}

function scheduleArtPut(personaId: string, patch: Partial<PersonaArt>, prev?: PersonaArt) {
  const backendPatch = toBackendArtPatch(patch, prev);
  if (!Object.keys(backendPatch).length) return;
  const queued = pendingArtPatch[personaId];
  pendingArtPatch[personaId] = {
    ...queued,
    ...backendPatch,
    // Позы копятся поштучно: null из предыдущей правки не теряется
    ...(backendPatch.sprites && queued?.sprites ? { sprites: { ...queued.sprites, ...backendPatch.sprites } } : {}),
  };
  clearTimeout(artPutTimers[personaId]);
  artPutTimers[personaId] = setTimeout(() => {
    delete artPutTimers[personaId];
    if (!online) return; // офлайн — патч остаётся в очереди, уйдёт при реконнекте
    const body = pendingArtPatch[personaId];
    delete pendingArtPatch[personaId];
    api.putRoomArt(personaId, body).catch(() => {
      // бэкенд недоступен/ошибка — локальный кеш остаётся источником истины
    });
  }, ART_PUT_DEBOUNCE_MS);
}

function scheduleStylePut(personaId: string, patch: { description?: string; reference?: string | null }) {
  if (!Object.keys(patch).length) return;
  pendingStylePatch[personaId] = { ...pendingStylePatch[personaId], ...patch };
  clearTimeout(stylePutTimers[personaId]);
  stylePutTimers[personaId] = setTimeout(() => {
    delete stylePutTimers[personaId];
    if (!online) return;
    const body = pendingStylePatch[personaId];
    delete pendingStylePatch[personaId];
    api.putRoomStyle(personaId, body).catch(() => {});
  }, STYLE_PUT_DEBOUNCE_MS);
}

// Патчи, накопленные пока бэкенд был недоступен, отправляем сразу по реконнекту
// (не дожидаясь следующей правки пользователя)
function flushPending(personaId: string) {
  if (pendingArtPatch[personaId] && !artPutTimers[personaId]) {
    const body = pendingArtPatch[personaId];
    delete pendingArtPatch[personaId];
    api.putRoomArt(personaId, body).catch(() => {});
  }
  if (pendingStylePatch[personaId] && !stylePutTimers[personaId]) {
    const body = pendingStylePatch[personaId];
    delete pendingStylePatch[personaId];
    api.putRoomStyle(personaId, body).catch(() => {});
  }
}

// Первая синхронизация персоны: сервер выигрывает там, где у него уже есть
// данные; локальные поля, которых на сервере ещё нет (офлайн-работа до
// первого подключения), не стираем — а выгружаем наверх
function ensureServerSync(personaId: string) {
  if (fetchedPersonas.has(personaId) || inflightPersonas.has(personaId)) return;
  inflightPersonas.add(personaId);
  Promise.allSettled([api.getRoomArt(personaId), api.getRoomStyle(personaId)]).then(([artRes, styleRes]) => {
    inflightPersonas.delete(personaId);
    // Обе точки недоступны — не помечаем синхронизированным, попробуем ещё раз позже
    if (artRes.status === 'rejected' && styleRes.status === 'rejected') return;
    fetchedPersonas.add(personaId);
    const local = state[personaId] ?? {};
    const merged: PersonaArt = { ...local };
    const artUpload: Partial<PersonaArt> = {};
    // Правка, сделанная пока шёл GET (ещё в очереди PUT), главнее ответа сервера
    const artQueued = pendingArtPatch[personaId] ?? {};
    const styleQueued = pendingStylePatch[personaId] ?? {};
    if (artRes.status === 'fulfilled') {
      const server = fromBackendArt(artRes.value);
      if (!('sprite' in artQueued)) {
        if (server.sprite) merged.sprite = server.sprite;
        else if (local.sprite) artUpload.sprite = local.sprite;
      }
      if (!('sprites' in artQueued)) {
        if (server.sprites) merged.sprites = server.sprites;
        else if (local.sprites) artUpload.sprites = local.sprites;
      }
      if (!('room_bg' in artQueued)) {
        if (server.roomBg) merged.roomBg = server.roomBg;
        else if (local.roomBg) artUpload.roomBg = local.roomBg;
      }
    }
    const styleUpload: { description?: string; reference?: string | null } = {};
    if (styleRes.status === 'fulfilled') {
      const server = styleRes.value;
      if (!('reference' in styleQueued)) {
        if (server.reference) merged.reference = server.reference;
        else if (local.reference) styleUpload.reference = local.reference;
      }
      if (!('description' in styleQueued)) {
        if (server.description) merged.styleDescription = server.description;
        else if (local.styleDescription) styleUpload.description = local.styleDescription;
      }
    }
    state = { ...state, [personaId]: merged };
    persist();
    listeners.forEach((l) => l());
    if (Object.keys(artUpload).length) scheduleArtPut(personaId, artUpload);
    if (Object.keys(styleUpload).length) scheduleStylePut(personaId, styleUpload);
  });
}

// Обновить арт персоны (частичный патч) и уведомить подписчиков; спрайты/фон
// уходят в art.json, референс/описание — в style.json (см. room_spec.md)
export function updatePersonaArt(personaId: string, patch: Partial<PersonaArt>) {
  const prev = state[personaId];
  state = { ...state, [personaId]: { ...prev, ...patch } };
  persist();
  listeners.forEach((l) => l());
  if ('sprite' in patch || 'sprites' in patch || 'roomBg' in patch) {
    scheduleArtPut(personaId, patch, prev);
  }
  if ('reference' in patch || 'styleDescription' in patch) {
    const stylePatch: { description?: string; reference?: string | null } = {};
    if ('reference' in patch) stylePatch.reference = patch.reference ?? null;
    if ('styleDescription' in patch) stylePatch.description = patch.styleDescription ?? '';
    scheduleStylePut(personaId, stylePatch);
  }
}

// Обновить только стиль (описание/референс) — удобная обёртка над updatePersonaArt
export function updatePersonaStyle(personaId: string, patch: Partial<PersonaStyle>) {
  const artPatch: Partial<PersonaArt> = {};
  if ('description' in patch) artPatch.styleDescription = patch.description;
  if ('reference' in patch) artPatch.reference = patch.reference;
  updatePersonaArt(personaId, artPatch);
}

// Смена id персоны: арт переезжает под новый id; незавершённые PUT под старый
// id не переносим — новый id синхронизируется с сервера заново
export function renamePersonaArt(oldId: string, newId: string) {
  if (!(oldId in state)) return;
  const { [oldId]: art, ...rest } = state;
  state = { ...rest, [newId]: art };
  persist();
  listeners.forEach((l) => l());
  clearTimeout(artPutTimers[oldId]);
  delete artPutTimers[oldId];
  delete pendingArtPatch[oldId];
  clearTimeout(stylePutTimers[oldId]);
  delete stylePutTimers[oldId];
  delete pendingStylePatch[oldId];
  fetchedPersonas.delete(oldId);
  fetchedPersonas.delete(newId);
}

// Память id ушла в архив (персона «с чистого листа»): забыть кеш арта под этим
// id — иначе синхронизация залила бы старый арт на сервер новой персоне
export function forgetPersonaArt(personaId: string) {
  clearTimeout(artPutTimers[personaId]);
  delete artPutTimers[personaId];
  delete pendingArtPatch[personaId];
  clearTimeout(stylePutTimers[personaId]);
  delete stylePutTimers[personaId];
  delete pendingStylePatch[personaId];
  fetchedPersonas.delete(personaId);
  if (!(personaId in state)) return;
  const { [personaId]: _dropped, ...rest } = state;
  state = rest;
  persist();
  listeners.forEach((l) => l());
}

// Хук-подписка на арт конкретной персоны; при наличии бэкенда — подтягивает
// art.json/style.json один раз на персону и досылает накопленные офлайн-правки
export function usePersonaArt(personaId: string): PersonaArt | undefined {
  const isOnline = useApiOnline();
  useEffect(() => {
    online = isOnline;
    if (isOnline) {
      ensureServerSync(personaId);
      flushPending(personaId);
    }
  }, [isOnline, personaId]);
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => state[personaId],
  );
}

// Стиль персонажа (описание + референс) тем же стором — для мастерской и промпт-паков
export function usePersonaStyle(personaId: string): PersonaStyle {
  const art = usePersonaArt(personaId);
  return { description: art?.styleDescription ?? '', reference: art?.reference };
}

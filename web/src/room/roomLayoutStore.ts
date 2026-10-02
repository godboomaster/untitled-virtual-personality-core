/* Раскладка комнаты на бэкенде (layout.json: метки/размеры/иконки/картинки
   предметов, места вокруг них, аватар). Грузится один раз на персону (не
   поллится), правки применяются локально сразу и уходят PUT /room/layout
   одним пакетом с дебаунсом ~800 мс — перетаскивание метки не шлёт запрос
   на каждый кадр. */

import { useEffect, useSyncExternalStore } from 'react';
import { api } from '../api';
import type { RoomLayout, RoomLayoutItem, RoomLayoutPatch } from './roomTypes';

const SAVE_DEBOUNCE_MS = 800;

const layouts = new Map<string, RoomLayout>();
const loading = new Set<string>();
const loadedAt = new Map<string, number>();
const listeners = new Set<() => void>();
const pending = new Map<string, RoomLayoutPatch>();
const saveTimers = new Map<string, ReturnType<typeof setTimeout>>();
const saving = new Set<string>();
// Счётчик отправленных PUT: ответ GET, начатого до записи, устарел
const putCount = new Map<string, number>();

// Повторная загрузка раскладки при новом монтировании — не чаще
const RELOAD_MS = 5 * 60_000;

function emit() {
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

// Слить патч в раскладку (null у предмета — удалить запись)
function applyPatch(base: RoomLayout, patch: RoomLayoutPatch): RoomLayout {
  const items = { ...base.items };
  for (const [name, p] of Object.entries(patch.items ?? {})) {
    if (p === null) delete items[name];
    else items[name] = { ...items[name], ...p };
  }
  return {
    ...base,
    items,
    ...('avatar' in patch ? { avatar: patch.avatar ?? null } : {}),
  };
}

// Накопить патч к ещё не отправленному
function mergePatch(a: RoomLayoutPatch | undefined, b: RoomLayoutPatch): RoomLayoutPatch {
  if (!a) return b;
  const items: Record<string, Partial<RoomLayoutItem> | null> = { ...(a.items ?? {}) };
  for (const [name, p] of Object.entries(b.items ?? {})) {
    const prev = items[name];
    items[name] = p === null ? null : { ...(prev ?? {}), ...p };
  }
  return {
    ...(Object.keys(items).length ? { items } : {}),
    ...('avatar' in a ? { avatar: a.avatar } : {}),
    ...('avatar' in b ? { avatar: b.avatar } : {}),
  };
}

function ensureLoaded(persona: string) {
  if (loading.has(persona)) return;
  const at = loadedAt.get(persona);
  if (at && Date.now() - at < RELOAD_MS) return;
  loading.add(persona);
  const puts = putCount.get(persona) ?? 0;
  api
    .getRoomLayout(persona)
    .then((layout) => {
      loadedAt.set(persona, Date.now());
      // Пока шёл GET, ушёл PUT — его ответ свежее (или ещё придёт)
      if ((putCount.get(persona) ?? 0) !== puts) return;
      // Неотправленные правки поверх свежей раскладки
      const p = pending.get(persona);
      layouts.set(persona, p ? applyPatch(normalize(layout), p) : normalize(layout));
      emit();
    })
    .catch(() => {
      // Эндпоинта нет (старый бэкенд) или сеть — работаем без раскладки
    })
    .finally(() => loading.delete(persona));
}

function normalize(l: Partial<RoomLayout> | null | undefined): RoomLayout {
  return { items: l?.items ?? {}, avatar: l?.avatar ?? null, ...(l?.updated_at ? { updated_at: l.updated_at } : {}) };
}

function flush(persona: string) {
  saveTimers.delete(persona);
  if (saving.has(persona)) {
    // Предыдущий PUT ещё в полёте — повторим после него
    saveTimers.set(persona, setTimeout(() => flush(persona), SAVE_DEBOUNCE_MS));
    return;
  }
  const patch = pending.get(persona);
  if (!patch) return;
  pending.delete(persona);
  saving.add(persona);
  putCount.set(persona, (putCount.get(persona) ?? 0) + 1);
  api
    .putRoomLayout(persona, patch)
    .then((server) => {
      // Пока шёл запрос, могли накопиться новые правки — локальное состояние главнее
      if (!pending.has(persona)) {
        layouts.set(persona, normalize(server));
        emit();
      }
    })
    .catch(() => {
      // Не сохранилось — правка остаётся локальной до следующей загрузки
      // (следующее монтирование перечитает раскладку с сервера)
      loadedAt.delete(persona);
    })
    .finally(() => saving.delete(persona));
}

// Правка раскладки: сразу локально + отложенный PUT
export function patchRoomLayout(persona: string, patch: RoomLayoutPatch) {
  const base = layouts.get(persona) ?? normalize(null);
  layouts.set(persona, applyPatch(base, patch));
  pending.set(persona, mergePatch(pending.get(persona), patch));
  const t = saveTimers.get(persona);
  if (t) clearTimeout(t);
  saveTimers.set(persona, setTimeout(() => flush(persona), SAVE_DEBOUNCE_MS));
  emit();
}

// Раскладка персоны; null — ещё не загружена или бэкенд без /room/layout
export function useRoomLayout(persona: string, enabled: boolean): RoomLayout | null {
  useEffect(() => {
    if (enabled) ensureLoaded(persona);
  }, [persona, enabled]);
  const layout = useSyncExternalStore(subscribe, () => layouts.get(persona) ?? null);
  return enabled ? layout : null;
}

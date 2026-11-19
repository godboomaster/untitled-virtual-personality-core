/* Аватары персон, заданные во вкладке «Персоны». Хранятся на сервере
   (data/api_<id>/avatar.*) — общие для всех браузеров и переезжают вместе с
   папкой при смене id. localStorage (ключ vpc-persona-avatars) — только кеш
   для мгновенной отрисовки и мок-режима без бэкенда. Используются в карточках
   персон, списках чатов и иконках уведомлений. При загрузке картинка
   сжимается до квадрата 256×256.
   Без связи с ядром (телефон без компьютера, персона, созданная на телефоне)
   аватар задаётся в кеше и ждёт отправки (PENDING_KEY): при связи уходит на
   ядро (syncPendingAvatars — offline/state.ts), id персоны сменился при
   переносе — переезжает под новый (moveAvatar). */

import { useEffect, useReducer } from 'react';
import { api, ApiError } from './api';
import { isLocalPersona } from './offline/localPersonas';

const STORAGE_KEY = 'vpc-persona-avatars';
// Разовый перенос аватаров, заданных до хранения на сервере, из этого браузера
const MIGRATED_KEY = 'vpc-persona-avatars-migrated';
// Заданные/снятые без связи с ядром: id → set | clear
const PENDING_KEY = 'vpc-persona-avatars-pending';
const AVATAR_SIZE = 256;

type PendingOp = 'set' | 'clear';

function readPending(): Record<string, PendingOp> {
  try {
    const v = JSON.parse(localStorage.getItem(PENDING_KEY) ?? '{}') as Record<string, PendingOp>;
    return v && typeof v === 'object' ? v : {};
  } catch {
    return {};
  }
}

function writePending(next: Record<string, PendingOp>) {
  try {
    if (Object.keys(next).length) localStorage.setItem(PENDING_KEY, JSON.stringify(next));
    else localStorage.removeItem(PENDING_KEY);
  } catch {
    /* хранилище недоступно — отправится, если не перезапустят */
  }
}

function markPending(id: string, op: PendingOp | null) {
  const next = readPending();
  if (op) next[id] = op;
  else delete next[id];
  writePending(next);
}

// Ядра нет (сеть) или персоны на нём ещё нет (создана на телефоне) — задать
// в кеше и отправить при связи, а не откатывать
const offlineError = (personaId: string, e: unknown) =>
  !(e instanceof ApiError) || (e.status === 404 && isLocalPersona(personaId));

let avatars: Record<string, string> = (() => {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Record<string, string>;
  } catch {
    return {};
  }
})();

const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(avatars));
  } catch {
    // квота localStorage — кеш не критичен, источник правды на сервере
  }
}

function update(next: Record<string, string>) {
  avatars = next;
  persist();
  emit();
}

// ── Синхронизация с сервером ──
let fetched = false;
let inflight = false;

async function syncFromServer() {
  if (inflight) return;
  inflight = true;
  try {
    const { avatars: server } = await api.getPersonaAvatars();
    // Первый запуск после перехода на серверное хранение: отправляем то, что
    // лежало только в этом браузере (если у персоны на сервере аватара нет)
    let migrated = false;
    try {
      migrated = localStorage.getItem(MIGRATED_KEY) === '1';
    } catch {
      migrated = true;
    }
    // Не отправленные (сбой сети/сервера) остаются в кеше, флаг «перенесено»
    // ставится только когда отправлено всё — иначе аватар бы молча пропал
    const pending: Record<string, string> = {};
    if (!migrated) {
      const uploads = Object.entries(avatars).filter(([id]) => !(id in server));
      const results = await Promise.allSettled(uploads.map(([id, url]) => api.setPersonaAvatar(id, url)));
      results.forEach((r, i) => {
        const [id, url] = uploads[i];
        if (r.status === 'fulfilled') server[id] = url;
        // 404 — персоны с таким id уже нет (удалена): переносить некуда
        else if (!(r.reason instanceof ApiError && r.reason.status === 404)) pending[id] = url;
      });
      if (!Object.keys(pending).length) {
        try {
          localStorage.setItem(MIGRATED_KEY, '1');
        } catch {
          /* нет доступа к storage — повторим в следующий раз */
        }
      }
    }
    // Заданные без связи — на ядро; персоны с телефона там ещё нет — ждут
    const queued = readPending();
    for (const [id, op] of Object.entries(queued)) {
      if (isLocalPersona(id)) continue;
      try {
        if (op === 'set' && avatars[id]) {
          await api.setPersonaAvatar(id, avatars[id]);
          server[id] = avatars[id];
        } else if (op === 'clear') {
          await api.deletePersonaAvatar(id).catch((e) => {
            if (!(e instanceof ApiError && e.status === 404)) throw e;
          });
          delete server[id];
        }
        delete queued[id];
      } catch (e) {
        // 404 — персоны на ядре нет (удалена): отправлять некуда
        if (e instanceof ApiError && e.status === 404) delete queued[id];
      }
    }
    writePending(queued);
    for (const [id, op] of Object.entries(queued)) {
      if (op === 'set' && avatars[id]) pending[id] = avatars[id];
      else if (op === 'clear') delete server[id];
    }
    fetched = true;
    update({ ...server, ...pending });
  } catch {
    // бэкенд недоступен — работаем с кешем браузера (мок-режим)
  } finally {
    inflight = false;
  }
}

// Перечитать аватары с сервера (после смены id, удаления персоны)
export function refetchAvatars() {
  fetched = false;
  void syncFromServer();
}

/** Ядро снова на связи: заданные без него аватары — туда. */
export async function syncPendingAvatars(): Promise<void> {
  if (!Object.keys(readPending()).length && fetched) return;
  fetched = false;
  await syncFromServer();
}

/** id персоны сменился при переносе на ядро — аватар под новым. */
export function moveAvatar(from: string, to: string) {
  const queued = readPending();
  if (from in queued) {
    queued[to] = queued[from];
    delete queued[from];
    writePending(queued);
  }
  if (!(from in avatars)) return;
  const next = { ...avatars, [to]: avatars[from] };
  delete next[from];
  update(next);
}

/** Персона с телефона удалена — её аватар тоже (на ядре его нет). */
export function forgetAvatar(id: string) {
  markPending(id, null);
  if (!(id in avatars)) return;
  const rest = { ...avatars };
  delete rest[id];
  update(rest);
}

// Подписка на хранилище аватаров (обновляется сразу после загрузки)
export function usePersonaAvatars(): Record<string, string> {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    if (!fetched) void syncFromServer();
    return () => {
      listeners.delete(force);
    };
  }, []);
  return avatars;
}

export function getPersonaAvatar(personaId: string): string | null {
  return avatars[personaId] ?? null;
}

// Задать аватар: сразу в UI, затем на сервер. Сервер недоступен —
// остаётся в кеше браузера (мок-режим); ошибка сервера — откат и reject
export function setPersonaAvatar(personaId: string, dataUrl: string): Promise<void> {
  const prev = avatars[personaId];
  update({ ...avatars, [personaId]: dataUrl });
  if (!fetched || isLocalPersona(personaId)) {
    markPending(personaId, 'set');
    return Promise.resolve();
  }
  return api.setPersonaAvatar(personaId, dataUrl).then(
    () => markPending(personaId, null),
    (e) => {
      if (offlineError(personaId, e)) {
        markPending(personaId, 'set');
        return;
      }
      const rest = { ...avatars };
      if (prev) rest[personaId] = prev;
      else delete rest[personaId];
      update(rest);
      throw e;
    },
  );
}

export function clearPersonaAvatar(personaId: string): Promise<void> {
  if (!(personaId in avatars)) return Promise.resolve();
  const prev = avatars[personaId];
  const rest = { ...avatars };
  delete rest[personaId];
  update(rest);
  if (isLocalPersona(personaId)) {
    markPending(personaId, null);
    return Promise.resolve();
  }
  if (!fetched) {
    markPending(personaId, 'clear');
    return Promise.resolve();
  }
  return api.deletePersonaAvatar(personaId).then(
    () => markPending(personaId, null),
    (e) => {
      if (offlineError(personaId, e)) {
        markPending(personaId, 'clear');
        return;
      }
      update({ ...avatars, [personaId]: prev });
      throw e;
    },
  );
}

// Файл → квадратный data-URL 256×256 (cover-кроп). PNG-оригиналы
// сохраняют прозрачность, остальные кодируются в JPEG.
export function fileToAvatarDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    if (!file.type.startsWith('image/')) {
      reject(new Error('not an image'));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('read error'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('decode error'));
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = AVATAR_SIZE;
        canvas.height = AVATAR_SIZE;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
          reject(new Error('no canvas'));
          return;
        }
        // Кроп по центру в квадрат
        const side = Math.min(img.width, img.height);
        const sx = (img.width - side) / 2;
        const sy = (img.height - side) / 2;
        ctx.drawImage(img, sx, sy, side, side, 0, 0, AVATAR_SIZE, AVATAR_SIZE);
        const keepAlpha = file.type === 'image/png' || file.type === 'image/svg+xml';
        resolve(canvas.toDataURL(keepAlpha ? 'image/png' : 'image/jpeg', 0.9));
      };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  });
}

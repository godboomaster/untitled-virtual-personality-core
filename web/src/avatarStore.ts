/* Аватары персон, заданные во вкладке «Персоны». Хранятся на сервере
   (data/api_<id>/avatar.*) — общие для всех браузеров и переезжают вместе с
   папкой при смене id. localStorage (ключ vpc-persona-avatars) — только кеш
   для мгновенной отрисовки и мок-режима без бэкенда. Используются в карточках
   персон, списках чатов и иконках уведомлений. При загрузке картинка
   сжимается до квадрата 256×256. */

import { useEffect, useReducer } from 'react';
import { api, ApiError } from './api';

const STORAGE_KEY = 'vpc-persona-avatars';
// Разовый перенос аватаров, заданных до хранения на сервере, из этого браузера
const MIGRATED_KEY = 'vpc-persona-avatars-migrated';
const AVATAR_SIZE = 256;

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
    fetched = true;
    update({ ...pending, ...server });
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
  if (!fetched) return Promise.resolve();
  return api.setPersonaAvatar(personaId, dataUrl).then(
    () => undefined,
    (e) => {
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
  if (!fetched) return Promise.resolve();
  return api.deletePersonaAvatar(personaId).then(
    () => undefined,
    (e) => {
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

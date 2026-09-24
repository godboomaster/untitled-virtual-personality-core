/* Аватары персон, заданные во вкладке «Персоны»: data-URL картинки в
   localStorage (ключ vpc-persona-avatars), переживают перезагрузку.
   Используются в карточках персон, списках чатов и иконках уведомлений.
   Хранилище локальное для браузера; при загрузке картинка сжимается
   до квадрата 256×256, чтобы не раздувать localStorage. */

import { useEffect, useReducer } from 'react';

const STORAGE_KEY = 'vpc-persona-avatars';
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
  localStorage.setItem(STORAGE_KEY, JSON.stringify(avatars));
}

// Подписка на хранилище аватаров (обновляется сразу после загрузки)
export function usePersonaAvatars(): Record<string, string> {
  const [, force] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    listeners.add(force);
    return () => {
      listeners.delete(force);
    };
  }, []);
  return avatars;
}

export function getPersonaAvatar(personaId: string): string | null {
  return avatars[personaId] ?? null;
}

export function setPersonaAvatar(personaId: string, dataUrl: string) {
  avatars = { ...avatars, [personaId]: dataUrl };
  persist();
  emit();
}

export function clearPersonaAvatar(personaId: string) {
  if (!(personaId in avatars)) return;
  avatars = { ...avatars };
  delete avatars[personaId];
  persist();
  emit();
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

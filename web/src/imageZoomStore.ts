/* Стор лайтбокса картинок: клик по контентному изображению (сообщения чата,
   превью аттача, арт) открывает его крупным просмотром. Module-store на
   useSyncExternalStore, как artStore. Открытие доступно и вне React —
   клики по картинкам внутри sandboxed-скина приходят из SkinFrame
   postMessage'ом (скин сам окно открыть не может — opaque origin). */

import { useSyncExternalStore } from 'react';

let src: string | null = null;
const listeners = new Set<() => void>();

export function openImageZoom(next: string) {
  if (!next) return;
  src = next;
  listeners.forEach((l) => l());
}

export function closeImageZoom() {
  if (src === null) return;
  src = null;
  listeners.forEach((l) => l());
}

// Хук-подписка: src открытой картинки или null (лайтбокс закрыт)
export function useImageZoom(): string | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => src,
  );
}

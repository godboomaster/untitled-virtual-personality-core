import { useSyncExternalStore } from 'react';

/* Узкий экран (телефон) — та же граница, что в mobile.css: для раскладок,
   которые одним CSS не переставить (элемент в другом месте дерева). */

const QUERY = '(max-width: 760px)';

function subscribe(cb: () => void) {
  const mq = window.matchMedia(QUERY);
  mq.addEventListener('change', cb);
  return () => mq.removeEventListener('change', cb);
}

export function useNarrowScreen(): boolean {
  return useSyncExternalStore(subscribe, () => window.matchMedia(QUERY).matches);
}

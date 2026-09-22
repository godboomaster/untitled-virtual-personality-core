/* Режим разработчика: флаг в localStorage + подписка (useSyncExternalStore).
   Включается в «Настройки → Общие»; при включении App монтирует DevLogPanel. */

import { useSyncExternalStore } from 'react';

const KEY = 'vpc-dev-mode';
const listeners = new Set<() => void>();

export function isDevMode(): boolean {
  return localStorage.getItem(KEY) === '1';
}

export function setDevMode(on: boolean) {
  localStorage.setItem(KEY, on ? '1' : '0');
  listeners.forEach((l) => l());
}

export function useDevMode(): boolean {
  return useSyncExternalStore((cb) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }, isDevMode);
}

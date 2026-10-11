/* Режим разработчика: флаг в localStorage + подписка (useSyncExternalStore).
   Включается в «Настройки → Общие»; при включении App монтирует DevLogPanel
   (на телефоне — ещё и кнопку LOGS в шапке). */

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

// Панель логов развёрнута: общее для панели и кнопки в шапке (телефон)
let logOpen = false;

export function setDevLogOpen(on: boolean) {
  logOpen = on;
  listeners.forEach((l) => l());
}

export function useDevLogOpen(): boolean {
  return useSyncExternalStore((cb) => {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }, () => logOpen);
}

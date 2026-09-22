// Стор правок инвентаря персон: общий для разделов «Комната» и «Редактор».
// Пока персона не редактировалась, стор пуст и разделы показывают моки.
// Простой module-store на useSyncExternalStore по образцу artStore,
// персистентность — localStorage с защитой от переполнения квоты.
import { useSyncExternalStore } from 'react';
import type { InventoryItem } from './mockData';

type InventoryState = Record<string, InventoryItem[]>;

const STORAGE_KEY = 'vpc-inventory';

const listeners = new Set<() => void>();

function load(): InventoryState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as InventoryState) : {};
  } catch {
    return {};
  }
}

let state: InventoryState = load();

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Квота localStorage переполнена — правки живут только в рамках сессии
  }
}

// Заменить список предметов персоны и уведомить подписчиков
export function setPersonaItems(personaId: string, items: InventoryItem[]) {
  state = { ...state, [personaId]: items };
  persist();
  listeners.forEach((l) => l());
}

// Хук-подписка на правки инвентаря конкретной персоны (undefined — правок не было)
export function usePersonaItems(personaId: string): InventoryItem[] | undefined {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => state[personaId],
  );
}

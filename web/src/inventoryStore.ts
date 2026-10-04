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

// Перетаскивание метки офлайн зовёт setPersonaItems на каждый pointermove —
// запись в localStorage (вместе с картинками предметов) склеиваем в одну
let persistTimer: ReturnType<typeof setTimeout> | null = null;
function flushPersist() {
  if (!persistTimer) return;
  clearTimeout(persistTimer);
  persistTimer = null;
  persist();
}
if (typeof window !== 'undefined') window.addEventListener('pagehide', flushPersist);

// Заменить список предметов персоны и уведомить подписчиков
export function setPersonaItems(personaId: string, items: InventoryItem[]) {
  state = { ...state, [personaId]: items };
  if (!persistTimer) {
    persistTimer = setTimeout(() => {
      persistTimer = null;
      persist();
    }, 500);
  }
  listeners.forEach((l) => l());
}

// Смена id персоны: правки инвентаря переезжают под новый id
export function renamePersonaItems(oldId: string, newId: string) {
  if (!(oldId in state)) return;
  const { [oldId]: items, ...rest } = state;
  state = { ...rest, [newId]: items };
  persist();
  listeners.forEach((l) => l());
}

// Память id ушла в архив (персона «с чистого листа»): правки под этим id забыть
export function forgetPersonaItems(personaId: string) {
  if (!(personaId in state)) return;
  const { [personaId]: _dropped, ...rest } = state;
  state = rest;
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

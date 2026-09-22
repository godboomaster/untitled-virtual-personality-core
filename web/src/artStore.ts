// Стор пользовательского арта для персон: спрайт, фон комнаты и эталонный
// референс. Простой module-store на useSyncExternalStore — Art и Room
// видят одни данные. Персистентность — localStorage (dataURL), с защитой
// от переполнения квоты.
import { useSyncExternalStore } from 'react';

// Точка в долях кадра (0..1)
export interface AnchorPoint {
  x: number;
  y: number;
}

export interface SpriteAsset {
  dataUrl: string;
  anchor: AnchorPoint; // маркер «ступни» — точка контакта с полом
}

// Ключи точек пола совпадают с точками перемещения аватара в комнате
export interface RoomBgAsset {
  dataUrl: string;
  floorPoints: Record<'desk' | 'window' | 'shelf', AnchorPoint>;
}

export interface PersonaArt {
  sprite?: SpriteAsset;
  roomBg?: RoomBgAsset;
  reference?: string; // dataUrl эталонного референса
}

type ArtState = Record<string, PersonaArt>;

const STORAGE_KEY = 'vpc-art';

const listeners = new Set<() => void>();

function load(): ArtState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ArtState) : {};
  } catch {
    return {};
  }
}

let state: ArtState = load();

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Квота localStorage переполнена — арт живёт только в рамках сессии
  }
}

// Обновить арт персоны (частичный патч) и уведомить подписчиков
export function updatePersonaArt(personaId: string, patch: Partial<PersonaArt>) {
  state = { ...state, [personaId]: { ...state[personaId], ...patch } };
  persist();
  listeners.forEach((l) => l());
}

// Хук-подписка на арт конкретной персоны
export function usePersonaArt(personaId: string): PersonaArt | undefined {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => state[personaId],
  );
}

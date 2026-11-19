/* Аватар, собранный в этой сессии конструктором (офлайн — поверх мока).
   Общий для комнаты, её PiP-окна и аватара персоны в чатах: правка в
   конструкторе сразу видна везде. */

import { useSyncExternalStore } from 'react';
import type { RoomAvatarPreset } from '../mockData';

const sessionAvatars = new Map<string, RoomAvatarPreset>();
const listeners = new Set<() => void>();

export function setSessionAvatar(persona: string, preset: RoomAvatarPreset) {
  sessionAvatars.set(persona, preset);
  listeners.forEach((l) => l());
}

export function getSessionAvatar(persona: string): RoomAvatarPreset | undefined {
  return sessionAvatars.get(persona);
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useSessionAvatar(persona: string): RoomAvatarPreset | undefined {
  return useSyncExternalStore(subscribe, () => sessionAvatars.get(persona));
}

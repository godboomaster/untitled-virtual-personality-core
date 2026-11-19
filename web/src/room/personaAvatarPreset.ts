/* Облик персоны в «Комнате» — тот же путь, что у сцены (useRoomScene):
   правка конструктора в этой сессии → раскладка ядра (layout.json) →
   облик по умолчанию (avatarDefaults). По нему рисуются аватар персоны без
   своей картинки (PersonaAvatar), иконка её уведомлений и голова в виджете
   телефона (widgetHeads). */

import { useApiOnline } from '../apiData';
import type { RoomAvatarPreset } from '../mockData';
import { peekRoomLayout, useRoomLayout } from './roomLayoutStore';
import { fallbackAvatar } from './avatarDefaults';
import { getSessionAvatar, useSessionAvatar } from './sessionAvatar';

export function usePersonaAvatarPreset(persona: string): RoomAvatarPreset {
  const layout = useRoomLayout(persona, useApiOnline());
  const session = useSessionAvatar(persona);
  return session ?? layout?.avatar ?? fallbackAvatar(persona);
}

// Вне React (уведомления): раскладку берём, только если уже загружена
export function personaAvatarPreset(persona: string): RoomAvatarPreset {
  return getSessionAvatar(persona) ?? peekRoomLayout(persona)?.avatar ?? fallbackAvatar(persona);
}

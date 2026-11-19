/* Облик фигуры персоны, пока его не собрали в конструкторе: у встроенных
   персон он задан (был в моках комнаты), у остальных — по хэшу id. Общий
   для веба, виджета телефона и портрета на рабочем столе. */

import type { RoomAvatarPreset } from '../mockData';
import { defaultAvatarFor } from './roomModel';

export const BUILTIN_AVATARS: Record<string, RoomAvatarPreset> = {
  connor: { head: 'hex', eyes: 0, accessory: 0, shade: 1 },
  verso: { head: 'diamond', eyes: 2, accessory: 3, shade: 0 },
  assistant: { head: 'square', eyes: 1, accessory: 0, shade: 3 },
  verso_ru_group: { head: 'diamond', eyes: 2, accessory: 3, shade: 2 },
};

export function fallbackAvatar(persona: string): RoomAvatarPreset {
  return BUILTIN_AVATARS[persona] ?? defaultAvatarFor(persona);
}

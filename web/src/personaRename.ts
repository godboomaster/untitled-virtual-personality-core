// Смена id персоны целиком: бэкенд переносит YAML-файл, папку памяти (с
// аватаром), календарь и токен бота; здесь — всё, что браузер хранит под id
// (арт, инвентарь, скины, правки досье), и перечитывание списка персон.
import { api } from './api';
import { refetchPersonas } from './apiData';
import { renamePersonaArt } from './artStore';
import { refetchAvatars } from './avatarStore';
import { renamePersonaItems } from './inventoryStore';
import { renameOverlay } from './skins/overlayStore';
import { renameSkins } from './skins/skinStore';

// Формат id как у бэкенда (app/api/security.PERSONA_ID_RE)
export const PERSONA_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

// Промис резолвится, когда новый список персон уже загружен (родитель может
// сразу переключиться на newId); restartRequired — перенесён токен Telegram-бота
export async function renamePersonaId(oldId: string, newId: string): Promise<{ restartRequired: boolean }> {
  const r = await api.renamePersona(oldId, newId);
  renamePersonaArt(oldId, newId);
  renamePersonaItems(oldId, newId);
  renameSkins(oldId, newId);
  renameOverlay(oldId, newId);
  refetchAvatars();
  await refetchPersonas();
  return { restartRequired: !!r.restart_required };
}

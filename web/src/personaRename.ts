// Смена id персоны целиком: бэкенд переносит YAML-файл, папку памяти (с
// аватаром), календарь и токен бота; здесь — всё, что браузер хранит под id
// (арт, инвентарь, скины, правки досье), и перечитывание списка персон.
// Здесь же — выбор, что делать с памятью, оставшейся под id от удалённой
// персоны (создание и смена id).
import { api } from './api';
import type { MemoryChoice } from './api';
import { refetchPersonas } from './apiData';
import { forgetPersonaArt, renamePersonaArt } from './artStore';
import { refetchAvatars } from './avatarStore';
import { choiceDialog } from './dialogStore';
import { forgetPersonaItems, renamePersonaItems } from './inventoryStore';
import { forgetOverlay, renameOverlay } from './skins/overlayStore';
import { forgetPersonaSkins, renameSkins } from './skins/skinStore';

// Формат id как у бэкенда (app/api/security.PERSONA_ID_RE)
export const PERSONA_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

type TFn = (key: string, vars?: Record<string, string | number>) => string;

// Под id осталась память удалённой персоны (409 с memory_exists): подхватить /
// с чистого листа (старая — в архив, не удаляется) / отмена. canKeep=false —
// при смене id своя память персоны легла бы поверх: только архив или отмена.
// null — отмена
export async function askLeftoverMemory(t: TFn, id: string, canKeep = true): Promise<MemoryChoice | null> {
  const choice = await choiceDialog({
    title: t('personas.memoryTitle', { id }),
    message: t(canKeep ? 'personas.memoryMessage' : 'personas.memoryMessageNoKeep'),
    choices: [
      ...(canKeep ? [{ value: 'keep', label: t('personas.memoryKeep'), primary: true }] : []),
      { value: 'fresh', label: t('personas.memoryFresh'), primary: !canKeep },
    ],
  });
  return choice === 'keep' || choice === 'fresh' ? choice : null;
}

// Память id ушла в архив: всё, что браузер хранит под этим id (арт, правки
// инвентаря и досье, старые ключи и назначение скина), тоже не подмешиваем
export function forgetPersonaLocal(id: string) {
  forgetPersonaArt(id);
  forgetPersonaItems(id);
  forgetOverlay(id);
  forgetPersonaSkins(id);
}

// Промис резолвится, когда новый список персон уже загружен (родитель может
// сразу переключиться на newId); restartRequired — перенесён токен Telegram-бота.
// memory — выбор после 409 «под newId осталась память» (askLeftoverMemory)
export async function renamePersonaId(
  oldId: string,
  newId: string,
  memory?: MemoryChoice,
): Promise<{ restartRequired: boolean }> {
  const r = await api.renamePersona(oldId, newId, memory);
  if (memory === 'fresh') forgetPersonaLocal(newId);
  renamePersonaArt(oldId, newId);
  renamePersonaItems(oldId, newId);
  renameSkins(oldId, newId);
  renameOverlay(oldId, newId);
  refetchAvatars();
  await refetchPersonas();
  return { restartRequired: !!r.restart_required };
}

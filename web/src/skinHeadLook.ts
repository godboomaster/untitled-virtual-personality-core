import { getSkinAvatar } from '../skins';
import type { HeadLook } from './portrait/headLook';
import { appliedLook } from './skinTheme';

/* Рисовка головы персоны от применённого скина (web/skins/<id>/avatar.tsx).
   Живые головы берут её из контекста (SkinLookProvider), картинки вне дерева
   React — иконка уведомления, листы виджета — отсюда. */

const cache = new Map<string, HeadLook>();

export function headLookFor(skinId: string | null, scheme: 'light' | 'dark'): HeadLook | null {
  const avatar = getSkinAvatar(skinId);
  if (!avatar || !skinId) return null;
  const key = `${skinId}:${scheme}`;
  let look = cache.get(key);
  if (!look || look.draw !== avatar.draw) {
    look = { draw: avatar.draw, paint: avatar.paint, scheme, key };
    cache.set(key, look);
  }
  return look;
}

export function currentHeadLook(): HeadLook | null {
  const { skinId, scheme } = appliedLook();
  return headLookFor(skinId, scheme);
}

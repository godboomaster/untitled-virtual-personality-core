import { useEffect, useState } from 'react';
import { loadSkinAvatar, skinExists, skinSchemeFor } from '../../../web/skins/avatars';
import type { HeadLook } from '../../../web/src/portrait/headLook';

/* Голова портрета — рисовкой темы оформления, выбранной в вебе. Выбор живёт
   в браузере, сюда он приходит от ядра (app/core/ui_look): с портретом
   (portrait_load, поле look), а смена — событием ленты реплик, после
   которого портрет перечитывается. */

export interface UiLook {
  skin: string | null;
  personas?: Record<string, string>;
  theme?: 'light' | 'dark' | null;
}

// Своя тема персоны, иначе общая; «default» — у персоны обычный вид
export function skinOf(look: UiLook | null | undefined, persona: string | null | undefined): string | null {
  if (!look) return null;
  const own = persona ? look.personas?.[persona] : undefined;
  if (own === 'default') return null;
  if (own && skinExists(own)) return own;
  return skinExists(look.skin) ? look.skin : null;
}

function systemDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function useSystemDark(): boolean {
  const [dark, setDark] = useState(systemDark);
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = () => setDark(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return dark;
}

/** Рисовка головы: null — голова по умолчанию; undefined — рисовка скина
 *  ещё грузится при первом показе (голову пока не рисовать, чтобы не мелькнула
 *  чужая). При смене скина до загрузки новой остаётся прежняя. */
export function useSkinLook(look: UiLook | null | undefined, persona: string | null | undefined): HeadLook | null | undefined {
  const skin = skinOf(look, persona);
  const dark = useSystemDark();
  // Схема — выбор в шапке веба; ядро его не знает — как в системе
  const scheme = skinSchemeFor(skin, look?.theme ?? (dark ? 'dark' : 'light'));
  const key = skin ? `${skin}:${scheme}` : '';
  const [got, setGot] = useState<{ key: string; look: HeadLook | null } | null>(null);
  useEffect(() => {
    if (!skin) return;
    let alive = true;
    void loadSkinAvatar(skin).then((a) => {
      if (alive) setGot({ key, look: a ? { draw: a.draw, paint: a.paint, scheme, key } : null });
    });
    return () => {
      alive = false;
    };
  }, [skin, scheme, key]);
  if (!skin) return null;
  if (got) return got.look;
  return undefined;
}

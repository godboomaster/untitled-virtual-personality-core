import type { FigurePaintFn, HeadDraw } from '../src/portrait/headLook';

/* Рисовки головы скинов (web/skins/<id>/avatar.tsx) — отдельно от остального
   скина: их берёт и портрет на рабочем столе (desktop/), куда CSS, голос и
   части скинов не нужны. Реестр скинов (index.ts) грузит их отсюда же.
   Типы — напрямую из src/portrait/headLook: через ./types проверка типов
   панели потянула бы за собой весь веб. */

type Scheme = 'light' | 'dark';

export interface SkinAvatarModule {
  default: HeadDraw;
  figurePaint?: FigurePaintFn;
}

export const avatarLoaders = import.meta.glob<SkinAvatarModule>('./*/avatar.tsx');
const manifests = import.meta.glob<{ default: { schemes: Scheme[] } }>('./*/manifest.ts', { eager: true });

function manifestOf(id: string): { schemes: Scheme[] } | undefined {
  return manifests[`./${id}/manifest.ts`]?.default;
}

/** Есть ли такой скин */
export function skinExists(id: string | null | undefined): boolean {
  return !!id && !!manifestOf(id);
}

/** Схема при скине: выбранная, если скин её умеет, иначе его первая */
export function skinSchemeFor(id: string | null | undefined, wanted: Scheme): Scheme {
  const schemes = id ? manifestOf(id)?.schemes : undefined;
  if (!schemes || schemes.length === 0 || schemes.includes(wanted)) return wanted;
  return schemes[0];
}

const loaded = new Map<string, Promise<{ draw: HeadDraw; paint?: FigurePaintFn } | null>>();

/** Своя рисовка головы скина; у скина её нет (или не загрузилась) — null */
export function loadSkinAvatar(id: string): Promise<{ draw: HeadDraw; paint?: FigurePaintFn } | null> {
  let p = loaded.get(id);
  if (!p) {
    const loader = avatarLoaders[`./${id}/avatar.tsx`];
    p = loader
      ? loader().then(
          (m) => ({ draw: m.default, paint: m.figurePaint }),
          () => {
            loaded.delete(id); // сеть моргнула — следующая попытка загрузит заново
            return null;
          },
        )
      : Promise.resolve(null);
    loaded.set(id, p);
  }
  return p;
}

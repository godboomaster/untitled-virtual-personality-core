import type { SkinAmbient, SkinManifest } from './types';

/* Реестр скинов: всё, что лежит в web/skins/<id>/, подхватывается само.
   - manifest.ts — сразу (галерея, схемы);
   - swatch.css — сразу: миниатюра в галерее и @font-face скина (сами файлы
     шрифтов браузер качает, только когда шрифт понадобился);
   - skin.css — по требованию, когда скин впервые включают;
   - ambient.tsx — по требованию, если у скина есть живой фон. */

const manifestModules = import.meta.glob<{ default: SkinManifest }>('./*/manifest.ts', { eager: true });
import.meta.glob('./*/swatch.css', { eager: true });
const cssLoaders = import.meta.glob('./*/skin.css');
const ambientLoaders = import.meta.glob<{ default: SkinAmbient }>('./*/ambient.tsx');

function folderOf(path: string): string {
  return path.split('/')[1] ?? '';
}

export const SKINS: SkinManifest[] = Object.entries(manifestModules)
  .map(([path, mod]) => ({ ...mod.default, id: folderOf(path) }))
  .sort((a, b) => a.order - b.order);

const byId = new Map(SKINS.map((s) => [s.id, s]));

export function getSkin(id: string | null | undefined): SkinManifest | undefined {
  return id ? byId.get(id) : undefined;
}

const cssPromises = new Map<string, Promise<void>>();

// CSS скина грузится один раз; селекторы скина ограничены html[data-skin='<id>'],
// поэтому загруженный, но выключенный скин ничего не меняет
export function loadSkinCss(id: string): Promise<void> {
  let p = cssPromises.get(id);
  if (!p) {
    const loader = cssLoaders[`./${id}/skin.css`];
    p = loader
      ? loader().then(
          () => undefined,
          () => {
            cssPromises.delete(id); // сеть моргнула — следующая попытка загрузит заново
          },
        )
      : Promise.resolve();
    cssPromises.set(id, p);
  }
  return p;
}

export function hasAmbient(id: string): boolean {
  return `./${id}/ambient.tsx` in ambientLoaders;
}

export function loadAmbient(id: string): Promise<SkinAmbient | null> {
  const loader = ambientLoaders[`./${id}/ambient.tsx`];
  return loader ? loader().then((m) => m.default, () => null) : Promise.resolve(null);
}

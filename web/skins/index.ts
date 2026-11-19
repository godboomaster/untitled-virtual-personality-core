import { avatarLoaders } from './avatars';
import type { SkinAmbient, SkinAvatar, SkinFigurePaint, SkinManifest, SkinParts, SkinTexts } from './types';

/* Реестр скинов: всё, что лежит в web/skins/<id>/, подхватывается само.
   - manifest.ts — сразу (галерея, схемы);
   - swatch.css — сразу: миниатюра в галерее и @font-face скина (сами файлы
     шрифтов браузер качает, только когда шрифт понадобился);
   - skin.css — по требованию, когда скин впервые включают;
   - ambient.tsx — по требованию, если у скина есть живой фон;
   - texts.ts — вместе со skin.css: свой голос скина (строки интерфейса);
   - avatar.tsx — вместе со skin.css: своя рисовка головы персоны;
   - parts.tsx — вместе со skin.css: свои части интерфейса (окно задачи). */

const manifestModules = import.meta.glob<{ default: SkinManifest }>('./*/manifest.ts', { eager: true });
import.meta.glob('./*/swatch.css', { eager: true });
const cssLoaders = import.meta.glob('./*/skin.css');
const ambientLoaders = import.meta.glob<{ default: SkinAmbient }>('./*/ambient.tsx');
const textLoaders = import.meta.glob<{ default: SkinTexts }>('./*/texts.ts');
const textsById = new Map<string, SkinTexts>();
const avatarsById = new Map<string, { draw: SkinAvatar; paint?: SkinFigurePaint }>();
const partsLoaders = import.meta.glob<SkinParts>('./*/parts.tsx');
const partsById = new Map<string, SkinParts>();

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
    const textLoader = textLoaders[`./${id}/texts.ts`];
    // Голос скина грузится вместе с CSS: к моменту, когда скин встал на <html>,
    // его строки уже есть — интерфейс не мигает старыми формулировками
    const texts = textLoader
      ? textLoader().then((m) => {
          textsById.set(id, m.default);
        })
      : Promise.resolve();
    const avatarLoader = avatarLoaders[`./${id}/avatar.tsx`];
    const avatar = avatarLoader
      ? avatarLoader().then((m) => {
          avatarsById.set(id, { draw: m.default, paint: m.figurePaint });
        })
      : Promise.resolve();
    const partsLoader = partsLoaders[`./${id}/parts.tsx`];
    const parts = partsLoader
      ? partsLoader().then((m) => {
          partsById.set(id, { TaskCard: m.TaskCard, BrowserState: m.BrowserState, BootScreen: m.BootScreen });
        })
      : Promise.resolve();
    p = loader
      ? Promise.all([loader(), texts, avatar, parts]).then(
          () => undefined,
          () => {
            cssPromises.delete(id); // сеть моргнула — следующая попытка загрузит заново
          },
        )
      : Promise.all([texts, avatar, parts]).then(
          () => undefined,
          () => undefined,
        );
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

// Строки голоса скина (после loadSkinCss); нет файла texts.ts — undefined
export function getSkinTexts(id: string | null | undefined): SkinTexts | undefined {
  return id ? textsById.get(id) : undefined;
}

// Своя рисовка персоны (после loadSkinCss): голова и краска тела фигуры;
// нет avatar.tsx — undefined
export function getSkinAvatar(
  id: string | null | undefined,
): { draw: SkinAvatar; paint?: SkinFigurePaint } | undefined {
  return id ? avatarsById.get(id) : undefined;
}

// Свои части интерфейса скина (после loadSkinCss); нет parts.tsx — undefined
export function getSkinParts(id: string | null | undefined): SkinParts | undefined {
  return id ? partsById.get(id) : undefined;
}

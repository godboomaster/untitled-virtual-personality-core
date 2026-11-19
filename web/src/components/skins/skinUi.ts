// Общие мелочи панели скинов: шаблоны экранов, подписи, скачивание и
// копирование, проверка разметки готового файла экрана.

import skinTemplateChat from '../../skins/skin-template.chat.html?raw';
import skinTemplateDossier from '../../skins/skin-template.dossier.html?raw';
import skinTemplateRoom from '../../skins/skin-template.room.html?raw';
import { validateSkin } from '../../skins/engine';
import type { SkinScreen } from '../../skins/engine';
import type { SkinEntry } from '../../skins/skinStore';

export const TEMPLATES: Record<SkinScreen, string> = {
  chat: skinTemplateChat,
  dossier: skinTemplateDossier,
  room: skinTemplateRoom,
};

export const SCREEN_NAME_KEY: Record<SkinScreen, string> = {
  chat: 'skin.previewChat',
  dossier: 'skin.previewDossier',
  room: 'skin.previewRoom',
};

export function downloadFile(name: string, content: string, type = 'text/html') {
  const url = URL.createObjectURL(new Blob([content], { type: type + ';charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// Имя скина для показа: у встроенных — перевод
export function skinTitle(entry: SkinEntry, t: (key: string) => string): string {
  if (entry.nameKey) {
    const tr = t(entry.nameKey);
    if (tr !== entry.nameKey) return tr;
  }
  return entry.name;
}

// Часть имени файла из названия скина
export function skinSlug(name: string): string {
  return (
    name
      .toLowerCase()
      .replace(/[^a-z0-9а-яё]+/gi, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 40) || 'skin'
  );
}

export function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  return Math.max(1, Math.round(bytes / 1024)) + ' KB';
}

// HTML-файлы из перетаскивания/выбора
export function htmlFiles(list: FileList | null | undefined): File[] {
  return Array.from(list ?? []).filter((f) => f.type === 'text/html' || /\.html?$/i.test(f.name));
}

// Копирование в буфер — общее (src/clipboard.ts)
export { copyText } from '../../clipboard';

// Hook-атрибуты, которые bridge заполняет и слушает: каждая точка основы
// должна дожить до результата (переносить и оборачивать можно, удалять — нет)
const HOOK_ATTRS = ['data-vpc', 'data-vpc-field', 'data-vpc-action', 'data-vpc-setting'] as const;
const HOOK_SELECTOR = HOOK_ATTRS.map((a) => `[${a}]`).join(',');
const MISSING_HOOKS_SHOWN = 30;

// Точки внутри узла, включая содержимое <template> (querySelectorAll в него не заходит)
function collectHooks(root: ParentNode, out: Set<string>) {
  root.querySelectorAll(HOOK_SELECTOR).forEach((el) => {
    for (const a of HOOK_ATTRS) {
      const v = el.getAttribute(a);
      if (v) out.add(`[${a}="${v}"]`);
    }
  });
  root.querySelectorAll('template').forEach((tpl) => collectHooks(tpl.content, out));
}

// Hook-точки экрана основы, которых нет в результате. У основы берём только
// её блок [data-vpc-screen=screen] (у legacy-файла в нём все три экрана)
export function missingHooks(base: string, html: string, screen: SkinScreen): string[] {
  const parser = new DOMParser();
  const baseDoc = parser.parseFromString(base, 'text/html');
  const want = new Set<string>();
  collectHooks(baseDoc.querySelector(`[data-vpc-screen="${screen}"]`) ?? baseDoc, want);
  const got = new Set<string>();
  collectHooks(parser.parseFromString(html, 'text/html'), got);
  return [...want].filter((h) => !got.has(h));
}

export function missingHooksError(lang: 'ru' | 'en', missing: string[]): string {
  const shown = missing.slice(0, MISSING_HOOKS_SHOWN).join(', ');
  const more = missing.length - MISSING_HOOKS_SHOWN;
  return lang === 'en'
    ? `Hook points of the base file are missing (${missing.length}): ${shown}${more > 0 ? ` and ${more} more` : ''}. ` +
        'Bring every one of them back: move, wrap and restyle the elements freely, but never remove or rename them — ' +
        'without them parts of the interface are not drawn.'
    : `Из базового файла пропали hook-точки (${missing.length}): ${shown}${more > 0 ? ` и ещё ${more}` : ''}. ` +
        'Верни их все: элементы можно переносить, оборачивать и перестилизовать, но не удалять и не переименовывать — ' +
        'без них часть интерфейса не рисуется.';
}

/** Проверка разметки готового файла экрана: файл содержит свой экран,
 *  проходит validateSkin и сохраняет все hook-точки основы (base). Ошибки
 *  сформулированы для нейросети-редактора. */
export function checkSkinMarkup(
  html: string,
  screen: SkinScreen,
  base: string,
  lang: 'ru' | 'en',
  t: (key: string, vars?: Record<string, string | number>) => string,
): string[] {
  const v = validateSkin(html);
  const errs: string[] = [];
  if (!v.screens.includes(screen)) {
    errs.push(t('skin.errScreenMissing', { file: 'skin', screen: t(SCREEN_NAME_KEY[screen]), id: screen }));
  }
  const missing = missingHooks(base, html, screen);
  if (missing.length) errs.push(missingHooksError(lang, missing));
  return [...errs, ...v.errors];
}

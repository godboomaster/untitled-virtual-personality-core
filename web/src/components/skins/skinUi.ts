// Общие мелочи панели скинов: шаблоны экранов, подписи, скачивание файлов.

import skinTemplateChat from '../../skins/skin-template.chat.html?raw';
import skinTemplateDossier from '../../skins/skin-template.dossier.html?raw';
import skinTemplateRoom from '../../skins/skin-template.room.html?raw';
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

export function downloadFile(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/html' }));
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

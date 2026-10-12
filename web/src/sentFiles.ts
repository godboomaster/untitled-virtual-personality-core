import { registerPlugin } from '@capacitor/core';
import { fetchSentFile, isNativeApp } from './api';
import type { SentFile } from './api';

/* Файл, присланный ядром в чат («пришли файл X»): в браузере — обычное
   скачивание, в приложении на телефоне — плагин FileSave (в WebView
   <a download> с blob-ссылкой не работает): «Загрузки/Virtual Persona»,
   дальше — открыть приложением телефона. */

interface FileSavePlugin {
  begin(o: { name: string; mime: string }): Promise<{ id: string }>;
  append(o: { id: string; data: string }): Promise<void>;
  finish(o: { id: string }): Promise<{ uri: string; name: string; mime: string }>;
  cancel(o: { id: string }): Promise<void>;
  open(o: { uri: string; mime: string }): Promise<void>;
}

let plugin: FileSavePlugin | null = null;
const native = (): FileSavePlugin => (plugin ??= registerPlugin<FileSavePlugin>('FileSave'));

// Часть файла на один вызов моста: base64 растёт на треть, мост гоняет строку
const CHUNK = 512 * 1024;

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

export interface SavedFile {
  uri?: string; // в приложении: content:// сохранённой копии
  mime: string;
}

export async function saveSentFile(file: SentFile): Promise<SavedFile> {
  const blob = await fetchSentFile(file);
  const mime = blob.type || 'application/octet-stream';
  if (!isNativeApp()) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = file.name;
    a.click();
    // Освобождаем ссылку, когда браузер её уже забрал
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return { mime };
  }
  const p = native();
  const { id } = await p.begin({ name: file.name, mime });
  try {
    for (let off = 0; off < blob.size; off += CHUNK) {
      const part = await blob.slice(off, off + CHUNK).arrayBuffer();
      await p.append({ id, data: toBase64(part) });
    }
    const res = await p.finish({ id });
    return { uri: res.uri, mime: res.mime || mime };
  } catch (e) {
    await p.cancel({ id }).catch(() => {});
    throw e;
  }
}

export function openSavedFile(saved: SavedFile): Promise<void> {
  if (!saved.uri) return Promise.resolve();
  return native().open({ uri: saved.uri, mime: saved.mime });
}

// Нет приложения, которое открыло бы такой файл (ответ плагина)
export function isNoAppError(e: unknown): boolean {
  const code = (e as { code?: string; message?: string } | null);
  return code?.code === 'no_app' || code?.message === 'no_app';
}

export function formatFileSize(n: number, lang: string): string {
  const units = lang === 'ru' ? ['Б', 'КБ', 'МБ', 'ГБ'] : ['B', 'KB', 'MB', 'GB'];
  let x = n;
  let u = 0;
  while (x >= 1024 && u < units.length - 1) {
    x /= 1024;
    u += 1;
  }
  const s = u === 0 || x >= 10 ? String(Math.round(x)) : x.toFixed(1);
  return `${lang === 'ru' ? s.replace('.', ',') : s} ${units[u]}`;
}

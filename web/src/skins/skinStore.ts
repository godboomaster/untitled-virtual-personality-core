/* Хранилище скинов персон: файлы скинов и флаги «экран сломан» живут в
   localStorage, привязаны к id персоны. Скин персоны — до трёх отдельных
   файлов (чат, досье, комната); отсутствующий или сломанный экран
   показывается дефолтным UI, остальные остаются кастомными.

   Формат записи: JSON {"chat": html, "dossier": html, "room": html}.
   Legacy-формат (один комбинированный HTML под ключом) мигрирует на
   чтении: файл содержит все экраны, поэтому один и тот же документ
   раздаётся всем трём слотам. React-хук подписывается на изменения,
   чтобы чат/комната переключались мгновенно. */

import { useCallback, useEffect, useState } from 'react';
import { detectScreens } from './engine';
import type { SkinScreen } from './engine';

const SKIN_PREFIX = 'vpc-skin:';
const BROKEN_PREFIX = 'vpc-skin-broken:';
const CHANGE_EVENT = 'vpc-skins-updated';

export type PersonaSkins = Partial<Record<SkinScreen, string>>;
export type BrokenMap = Partial<Record<SkinScreen, string>>;

function emitChange() {
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export function loadSkins(personaId: string): PersonaSkins {
  const raw = localStorage.getItem(SKIN_PREFIX + personaId);
  if (!raw) return {};
  // Legacy: один комбинированный файл — отдать его всем слотам,
  // какие экраны в нём реально есть
  if (!raw.startsWith('{')) {
    const skins: PersonaSkins = {};
    for (const s of detectScreens(raw)) skins[s] = raw;
    return skins;
  }
  try {
    return JSON.parse(raw) as PersonaSkins;
  } catch {
    return {};
  }
}

function saveSkins(personaId: string, skins: PersonaSkins) {
  if (Object.keys(skins).length === 0) {
    localStorage.removeItem(SKIN_PREFIX + personaId);
  } else {
    localStorage.setItem(SKIN_PREFIX + personaId, JSON.stringify(skins));
  }
}

// Применить загруженные файлы: каждый файл раскладывается по слотам
// экранов, которые в нём обнаружены (per-screen файл — один слот,
// комбинированный — все). Слоты без новых файлов не трогаем; флаги
// «сломан» для обновлённых экранов снимаются.
export function applySkinFiles(personaId: string, files: string[]) {
  const skins = loadSkins(personaId);
  const broken = loadBroken(personaId);
  for (const file of files) {
    for (const screen of detectScreens(file)) {
      skins[screen] = file;
      delete broken[screen];
    }
  }
  saveSkins(personaId, skins);
  saveBroken(personaId, broken);
  emitChange();
}

export function dropSkin(personaId: string) {
  localStorage.removeItem(SKIN_PREFIX + personaId);
  localStorage.removeItem(BROKEN_PREFIX + personaId);
  emitChange();
}

export function loadBroken(personaId: string): BrokenMap {
  const raw = localStorage.getItem(BROKEN_PREFIX + personaId);
  if (!raw) return {};
  // Legacy: одна строка ошибки на весь скин — отметить все экраны
  if (!raw.startsWith('{')) return { chat: raw, dossier: raw, room: raw };
  try {
    return JSON.parse(raw) as BrokenMap;
  } catch {
    return {};
  }
}

function saveBroken(personaId: string, broken: BrokenMap) {
  if (Object.keys(broken).length === 0) {
    localStorage.removeItem(BROKEN_PREFIX + personaId);
  } else {
    localStorage.setItem(BROKEN_PREFIX + personaId, JSON.stringify(broken));
  }
}

export function markBroken(personaId: string, screen: SkinScreen, message: string) {
  const broken = loadBroken(personaId);
  broken[screen] = message;
  saveBroken(personaId, broken);
  emitChange();
}

export interface PersonaSkinState {
  // Файлы скина по экранам (нет ключа — экран показывается дефолтным UI)
  skins: PersonaSkins;
  // Тексты runtime-ошибок по экранам, из-за которых они отключены
  broken: BrokenMap;
  apply: (files: string[]) => void;
  reset: () => void;
  reportBroken: (screen: SkinScreen, message: string) => void;
}

export function usePersonaSkin(personaId: string): PersonaSkinState {
  const [, setTick] = useState(0);

  useEffect(() => {
    const bump = () => setTick((n) => n + 1);
    window.addEventListener(CHANGE_EVENT, bump);
    window.addEventListener('storage', bump);
    return () => {
      window.removeEventListener(CHANGE_EVENT, bump);
      window.removeEventListener('storage', bump);
    };
  }, []);

  const apply = useCallback((files: string[]) => applySkinFiles(personaId, files), [personaId]);
  const reset = useCallback(() => dropSkin(personaId), [personaId]);
  const reportBroken = useCallback(
    (screen: SkinScreen, message: string) => markBroken(personaId, screen, message),
    [personaId],
  );

  return {
    skins: loadSkins(personaId),
    broken: loadBroken(personaId),
    apply,
    reset,
    reportBroken,
  };
}

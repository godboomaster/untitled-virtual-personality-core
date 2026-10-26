/* Окно Document Picture-in-Picture поверх остальных окон: комната
   («Вынести комнату») или живой портрет («Вынести портрет»).
   Состояние — на уровне модуля, а рендер — в PipHost (смонтирован в
   App.tsx), поэтому окно живёт, пока пользователь ходит по другим разделам.
   Окно одно на приложение (у браузера такое окно одно на вкладку) и
   привязано к тому, что в нём показано, и к персоне: смена персоны во
   вкладках раздела его не трогает; «Вынести» у другой персоны или другого
   вида (портрет, пока вынесена комната) переключает уже открытое окно.

   В PiP-документ копируются стили приложения (link/style из <head>) и
   атрибуты <html> (data-theme, class, inline-переменные shell-темы скина);
   изменения темы и новые стили (HMR в dev) зеркалятся MutationObserver'ом.
   Закрытие окна пользователем (pagehide) — полная очистка. Браузеры без
   API (всё, кроме Chromium) — кнопка скрыта. */

import { useSyncExternalStore } from 'react';

interface PipRequestOptions {
  width?: number;
  height?: number;
  disallowReturnToOpener?: boolean;
}

interface DocumentPictureInPictureApi {
  requestWindow(opts?: PipRequestOptions): Promise<Window>;
  readonly window: Window | null;
}

function pipApi(): DocumentPictureInPictureApi | null {
  if (typeof window === 'undefined' || !('documentPictureInPicture' in window)) return null;
  return (window as unknown as { documentPictureInPicture: DocumentPictureInPictureApi }).documentPictureInPicture;
}

export const pipSupported = (): boolean => pipApi() != null;

export type PipKind = 'room' | 'portrait';

export interface PipState {
  win: Window | null;
  kind: PipKind | null;
  personaId: string | null;
}

// Размер содержимого окна: комната — широкая сцена, портрет — квадрат
const PIP_SIZE: Record<PipKind, { width: number; height: number }> = {
  room: { width: 420, height: 300 },
  portrait: { width: 300, height: 300 },
};

let state: PipState = { win: null, kind: null, personaId: null };
let teardown: (() => void) | null = null;
let opening = false;
const listeners = new Set<() => void>();

function set(next: PipState) {
  state = next;
  listeners.forEach((l) => l());
}

// Узлы стилей главного документа → копии в PiP
function isStyleNode(n: Node): n is HTMLLinkElement | HTMLStyleElement {
  return (
    n instanceof HTMLStyleElement || (n instanceof HTMLLinkElement && n.rel === 'stylesheet')
  );
}

// Копия узла стиля для PiP-документа: у <link> — абсолютный href (документ
// PiP — about:blank, относительный путь в нём мог бы не разрешиться)
function cloneStyleNode(n: HTMLLinkElement | HTMLStyleElement): Node {
  const copy = n.cloneNode(true);
  if (n instanceof HTMLLinkElement && copy instanceof HTMLLinkElement) copy.href = n.href;
  return copy;
}

function copyAttributes(from: Element, to: Element) {
  for (const a of Array.from(to.attributes)) {
    if (!from.hasAttribute(a.name)) to.removeAttribute(a.name);
  }
  for (const a of Array.from(from.attributes)) {
    if (to.getAttribute(a.name) !== a.value) to.setAttribute(a.name, a.value);
  }
}

function mirrorDocument(src: Document, dst: Document): () => void {
  src.head.querySelectorAll('link, style').forEach((n) => {
    if (isStyleNode(n)) dst.head.appendChild(cloneStyleNode(n));
  });
  copyAttributes(src.documentElement, dst.documentElement);
  dst.body.className = 'pip-body';

  // Смена темы/скина в приложении → те же атрибуты у PiP-документа
  const rootObs = new MutationObserver(() => copyAttributes(src.documentElement, dst.documentElement));
  rootObs.observe(src.documentElement, { attributes: true });
  // Стили, добавленные позже (ленивые чанки, HMR) — тоже в PiP; изменения
  // содержимого <style> в dev (HMR) — перезаливкой клона по data-атрибуту
  const headObs = new MutationObserver((records) => {
    for (const r of records) {
      r.addedNodes.forEach((n) => {
        if (isStyleNode(n)) dst.head.appendChild(cloneStyleNode(n));
      });
      const host = r.target instanceof HTMLStyleElement ? r.target : r.target.parentNode;
      if (host instanceof HTMLStyleElement && host.parentNode === src.head) {
        const id = host.getAttribute('data-vite-dev-id');
        if (id) {
          const twin = Array.from(dst.head.querySelectorAll('style')).find((s) => s.getAttribute('data-vite-dev-id') === id);
          if (twin) twin.textContent = host.textContent;
        }
      }
    }
  });
  headObs.observe(src.head, { childList: true, subtree: true, characterData: true });
  return () => {
    rootObs.disconnect();
    headObs.disconnect();
  };
}

// Открыть PiP (комнату или портрет персоны); уже открыто — переключить.
// Другой вид — окно и под его размер (resizeBy — по жесту, сейчас он есть)
export async function openPip(kind: PipKind, personaId: string): Promise<void> {
  const open = state.win;
  if (open && !open.closed) {
    if (state.kind !== kind) {
      try {
        open.resizeBy(PIP_SIZE[kind].width - open.innerWidth, PIP_SIZE[kind].height - open.innerHeight);
      } catch {
        // размер останется прежним — содержимое подстроится
      }
    }
    if (state.kind !== kind || state.personaId !== personaId) set({ ...state, kind, personaId });
    return;
  }
  const dpip = pipApi();
  if (!dpip || opening) return;
  opening = true;
  let win: Window;
  try {
    win = await dpip.requestWindow(PIP_SIZE[kind]);
  } catch {
    return; // отказ браузера (нет жеста пользователя и т.п.)
  } finally {
    opening = false;
  }
  const unmirror = mirrorDocument(document, win.document);
  const onPageHide = () => closePip();
  win.addEventListener('pagehide', onPageHide);
  teardown = () => {
    win.removeEventListener('pagehide', onPageHide);
    unmirror();
  };
  set({ win, kind, personaId });
}

// Закрыть PiP (кнопкой или pagehide): снять слушатели, размонтировать портал
export function closePip() {
  const win = state.win;
  teardown?.();
  teardown = null;
  if (win) set({ win: null, kind: null, personaId: null });
  if (win && !win.closed) win.close();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function usePip(): PipState {
  return useSyncExternalStore(subscribe, () => state);
}

// Вынесено ли сейчас именно это (вид и персона)
export function isPipOf(s: PipState, kind: PipKind, personaId: string): boolean {
  return s.win != null && s.kind === kind && s.personaId === personaId;
}

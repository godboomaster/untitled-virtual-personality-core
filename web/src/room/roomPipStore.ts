/* Окно Document Picture-in-Picture с комнатой («Вынести комнату»).
   Состояние — на уровне модуля, а рендер — в RoomPipHost (смонтирован в
   App.tsx), поэтому окно живёт, пока пользователь ходит по другим разделам.
   Окно одно на приложение и привязано к персоне, для которой открыто:
   смена персоны во вкладках раздела его не трогает; кнопка «Вынести
   комнату» у другой персоны переключает уже открытое окно на неё.

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

export const roomPipSupported = (): boolean => pipApi() != null;

export interface RoomPipState {
  win: Window | null;
  personaId: string | null;
}

const PIP_SIZE = { width: 420, height: 300 };

let state: RoomPipState = { win: null, personaId: null };
let teardown: (() => void) | null = null;
let opening = false;
const listeners = new Set<() => void>();

function set(next: RoomPipState) {
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
  dst.body.className = 'room-pip-body';

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

// Открыть PiP для персоны; уже открыто — переключить на неё
export async function openRoomPip(personaId: string): Promise<void> {
  if (state.win && !state.win.closed) {
    if (state.personaId !== personaId) set({ ...state, personaId });
    return;
  }
  const dpip = pipApi();
  if (!dpip || opening) return;
  opening = true;
  let win: Window;
  try {
    win = await dpip.requestWindow(PIP_SIZE);
  } catch {
    return; // отказ браузера (нет жеста пользователя и т.п.)
  } finally {
    opening = false;
  }
  const unmirror = mirrorDocument(document, win.document);
  const onPageHide = () => closeRoomPip();
  win.addEventListener('pagehide', onPageHide);
  teardown = () => {
    win.removeEventListener('pagehide', onPageHide);
    unmirror();
  };
  set({ win, personaId });
}

// Закрыть PiP (кнопкой или pagehide): снять слушатели, размонтировать портал
export function closeRoomPip() {
  const win = state.win;
  teardown?.();
  teardown = null;
  if (win) set({ win: null, personaId: null });
  if (win && !win.closed) win.close();
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useRoomPip(): RoomPipState {
  return useSyncExternalStore(subscribe, () => state);
}

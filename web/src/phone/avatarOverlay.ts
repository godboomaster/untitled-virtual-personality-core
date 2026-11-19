// Аватар поверх приложений (телефон): голова персоны в маленьком окне над
// любым приложением — AvatarOverlayService в web/android. Смотрит туда, где
// вы нажали, если включена служба спец. возможностей «Virtual Persona: куда
// вы нажимаете» (AvatarGazeService). Листы головы — те же, что у виджета
// «Живой портрет» (их печатает веб, portrait/widgetHeads.tsx).

import { registerPlugin } from '@capacitor/core';
import { isNativeApp } from '../api';

export interface AvatarOverlayStatus {
  canDraw: boolean; // право «Поверх других приложений»
  running: boolean; // окно на экране
  on: boolean; // включено (вернётся после перезагрузки)
  persona: string; // чья голова
  size: number; // сторона окна, dp
  error: string; // no_permission | no_sheets | start | ''
  passThrough: boolean; // «сквозь касания» включено вручную
  keyboard: boolean; // клавиатура открыта — голова сквозная сама
  sizeMin: number;
  sizeMax: number;
  gazeEnabled: boolean; // служба взгляда отмечена в спец. возможностях
  gazeRunning: boolean; // система её подняла
  hasSheets: boolean; // у спрошенной персоны есть листы головы на телефоне
  sdk: number;
}

interface AvatarOverlayPlugin {
  status(opts: { persona: string }): Promise<AvatarOverlayStatus>;
  start(opts: { persona: string; name: string; size: number }): Promise<void>;
  stop(): Promise<void>;
  setPassThrough(opts: { on: boolean }): Promise<void>;
  setPersonas(opts: { personas: { id: string; name: string }[] }): Promise<void>;
  openOverlaySettings(): Promise<void>;
  openGazeSettings(): Promise<void>;
}

const AvatarOverlay = registerPlugin<AvatarOverlayPlugin>('AvatarOverlay');

/** Сторона окна по умолчанию, dp (на экране — щипком двумя пальцами) */
export const OVERLAY_SIZE_DEFAULT = 104;

/** Сборка без плагина (старый APK) или не телефон — null. */
export async function avatarOverlayStatus(persona: string): Promise<AvatarOverlayStatus | null> {
  if (!isNativeApp()) return null;
  try {
    return await AvatarOverlay.status({ persona });
  } catch {
    return null;
  }
}

export function startAvatarOverlay(persona: string, name: string, size: number) {
  return AvatarOverlay.start({ persona, name, size });
}

export function setAvatarPassThrough(on: boolean) {
  return AvatarOverlay.setPassThrough({ on });
}

/** Имена персон — меню аватара «Сменить персону» (долгое нажатие на голову). */
export function setOverlayPersonas(personas: { id: string; name: string }[]) {
  if (!isNativeApp()) return;
  AvatarOverlay.setPersonas({ personas: personas.map((p) => ({ id: p.id, name: p.name })) }).catch(() => {
    /* старая сборка без плагина — меню возьмёт имена с диска */
  });
}

export function stopAvatarOverlay() {
  return AvatarOverlay.stop();
}

export function openOverlaySettings() {
  return AvatarOverlay.openOverlaySettings();
}

export function openGazeSettings() {
  return AvatarOverlay.openGazeSettings();
}

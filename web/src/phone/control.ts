// Управление экраном телефона (способ 2): служба спец. возможностей
// PhoneControlService в web/android — устройство ядра (app/core/devices).
// Что нажать, решает ядро (app/features/phone_control.py) по просьбе в любом
// чате: «на телефоне нажми «Отправить»», «что на экране телефона?». Отсюда —
// только адрес ядра и токен для службы, её состояние и пробный снимок для
// карточки в «Настройках».

import { registerPlugin } from '@capacitor/core';
import { getApiToken, getApiUrl, isLinkMode, isNativeApp } from '../api';

export type PhoneControlState = 'off' | 'no_core' | 'connecting' | 'online' | 'offline' | 'auth';

export interface PhoneControlStatus {
  enabled: boolean; // отмечена в настройках спец. возможностей
  running: boolean; // система её подняла
  state: PhoneControlState;
  error: string;
  lastSeen: number; // мс, последний ответ ядра
  lastCommand: string;
  configured: boolean; // адрес ядра у службы есть
  deviceId: string;
  sdk: number;
}

export interface ScreenElement {
  i: number;
  k: string; // button | item | field | switch | check | text | image | list
  t: string;
  h?: string;
  c?: number;
  ed?: number;
  pw?: number;
}

export interface ScreenSnapshot {
  snap: number;
  pkg?: string;
  app?: string;
  title?: string;
  locked?: boolean;
  screen_off?: boolean;
  more?: boolean;
  elements: ScreenElement[];
}

interface PhoneControlPlugin {
  status(): Promise<PhoneControlStatus>;
  configure(opts: { baseUrl: string; token: string }): Promise<void>;
  openSettings(): Promise<void>;
  openAppInfo(): Promise<void>;
  snapshot(): Promise<ScreenSnapshot>;
}

const PhoneControl = registerPlugin<PhoneControlPlugin>('PhoneControl');

/** Сборка без плагина (старый APK) — карточка скажет «обновите приложение». */
export async function phoneControlStatus(): Promise<PhoneControlStatus | null> {
  if (!isNativeApp()) return null;
  try {
    return await PhoneControl.status();
  } catch {
    return null;
  }
}

/** Ядро ответило: служба получает свежие адрес и токен. По VPC Link —
 * не работает (долгий опрос устройства идёт напрямую к ядру). */
export function syncPhoneControl() {
  if (!isNativeApp()) return;
  const direct = !isLinkMode() && !!getApiUrl();
  void PhoneControl.configure(direct ? { baseUrl: getApiUrl(), token: getApiToken() } : { baseUrl: '', token: '' }).catch(
    () => {},
  );
}

/** Смена сервера или отвязка: прежнее ядро служба больше не спрашивает. */
export function forgetPhoneControlServer() {
  if (!isNativeApp()) return;
  void PhoneControl.configure({ baseUrl: '', token: '' }).catch(() => {});
}

export function openPhoneControlSettings() {
  return PhoneControl.openSettings();
}

export function openAppInfo() {
  return PhoneControl.openAppInfo();
}

export function phoneScreenSnapshot() {
  return PhoneControl.snapshot();
}

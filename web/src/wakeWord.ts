import { registerPlugin } from '@capacitor/core';
import { getApiToken, getApiUrl, isLinkMode, isNativeApp } from './api';

/* Приложение на Android: «Слушать по имени» — скажите имя персоны
   («Коннор, какая погода?»), и она ответит голосом, даже при выключенном
   экране. Слушает нативная служба переднего плана с микрофоном
   (WakeWordService, плагин WakeWord в web/android): речь распознаётся на
   телефоне (sherpa-onnx), на ядро уходит только реплика с именем —
   POST /api/chat в тот же чат, что у приложения.

   Только для подключения по адресу и токену (как фоновые уведомления):
   по каналу VPC Link служба ходить не умеет. Выбор «включено» хранит сама
   служба (его можно снять и кнопкой «Выключить» в шторке); при запуске
   приложения ей передаются свежие адрес и токен. */

export type WakeState =
  | 'off'
  | 'loading'
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'paused'
  | 'no_names'
  | 'mic'
  | 'model';

export interface WakeStatus {
  enabled: boolean;
  running: boolean;
  state: WakeState;
  // Имена, на которые служба откликается (по одному на персону)
  names: string[];
  lastError: string | null;
  micGranted: boolean;
}

interface WakeWordPlugin {
  start(opts: { baseUrl: string; token: string }): Promise<{ started: boolean; reason?: 'mic-denied' }>;
  stop(): Promise<void>;
  configure(opts: { baseUrl: string; token: string }): Promise<void>;
  status(): Promise<WakeStatus>;
}

const WakeWord = registerPlugin<WakeWordPlugin>('WakeWord');

/** Переключатель есть: приложение на телефоне. */
export function wakeAvailable(): boolean {
  return isNativeApp();
}

export type WakeResult = 'ok' | 'mic-denied' | 'error';

/** Переключатель в «Настройки» → «Подключение»: включение спрашивает
 * микрофон (и уведомления на Android 13+) и запускает службу. */
export async function setWakeWord(on: boolean): Promise<WakeResult> {
  if (!isNativeApp()) return 'error';
  if (!on) {
    await WakeWord.stop().catch(() => {});
    return 'ok';
  }
  if (isLinkMode() || !getApiUrl()) return 'error';
  try {
    const r = await WakeWord.start({ baseUrl: getApiUrl(), token: getApiToken() });
    return r.started ? 'ok' : r.reason === 'mic-denied' ? 'mic-denied' : 'error';
  } catch {
    return 'error';
  }
}

export function wakeStatus(): Promise<WakeStatus> {
  return WakeWord.status();
}

/** Запуск приложения (ядро ответило): включено — служба получает свежие
 * адрес и токен и (пере)запускается; канал VPC Link — служба не нужна. */
export function syncWakeWord() {
  if (!isNativeApp()) return;
  void WakeWord.status()
    .then((s) => {
      if (!s.enabled) return;
      if (isLinkMode() || !getApiUrl()) return WakeWord.configure({ baseUrl: '', token: '' });
      return WakeWord.configure({ baseUrl: getApiUrl(), token: getApiToken() });
    })
    .catch(() => {});
}

/** Смена сервера или отвязка: прежнее ядро служба больше не спрашивает
 * (выбор «включено» остаётся — с новым адресом она запустится снова). */
export function forgetWakeServer() {
  if (!isNativeApp()) return;
  void WakeWord.configure({ baseUrl: '', token: '' }).catch(() => {});
}

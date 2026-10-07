import { registerPlugin } from '@capacitor/core';
import type { PluginListenerHandle } from '@capacitor/core';
import { getApiToken, getApiUrl, inboxClientId, isLinkMode, isNativeApp } from './api';
import { OPEN_CHAT_EVENT } from './notifications';

/* Приложение на Android: уведомления о сообщениях персон, когда приложение
   свёрнуто или закрыто. WebView в фоне не работает, поэтому опрос ядра
   (GET /api/inbox) делает нативная служба переднего плана
   (BackgroundInboxService, плагин BackgroundInbox в web/android). У неё свой
   курсор на ядре (clientId веба + "-bg"): что забрала служба, веб получит
   своим опросом как обычно.

   Только для подключения по адресу и токену: по каналу VPC Link служба
   ходить пока не умеет. Выбор «включено» помнится в localStorage; при
   каждом запуске приложения служба перезапускается со свежими адресом и
   токеном (их могли сменить на экране подключения). */

export interface BgInboxStatus {
  running: boolean;
  lastOk: number;
  lastError: string | null;
  // Уведомления приложению разрешены (Android 13+ — разрешение, раньше —
  // выключатель в настройках приложения)
  notificationsEnabled?: boolean;
}

interface BackgroundInboxPlugin {
  start(opts: { baseUrl: string; token: string; clientId: string }): Promise<void>;
  stop(): Promise<void>;
  status(): Promise<BgInboxStatus>;
  requestNotificationPermission(): Promise<{ granted: boolean }>;
  getLaunchPersona(): Promise<{ persona: string | null }>;
  addListener(event: 'notificationTap', cb: (data: { persona: string }) => void): Promise<PluginListenerHandle>;
}

const BackgroundInbox = registerPlugin<BackgroundInboxPlugin>('BackgroundInbox');

const ENABLED_KEY = 'vpc-bg-notify';

/** Переключатель есть: приложение на телефоне, подключение по адресу. */
export function bgNotifyAvailable(): boolean {
  return isNativeApp() && !isLinkMode();
}

/** Пользователь включил уведомления в фоне (выбор этого устройства). */
export function bgNotifyEnabled(): boolean {
  try {
    return localStorage.getItem(ENABLED_KEY) === '1';
  } catch {
    return false;
  }
}

function rememberEnabled(on: boolean) {
  try {
    if (on) localStorage.setItem(ENABLED_KEY, '1');
    else localStorage.removeItem(ENABLED_KEY);
  } catch {
    /* хранилище недоступно — выбор проживёт до перезапуска */
  }
}

// clientId — до 61 символа: служба допишет "-bg", а ядро принимает до 64
function startService(): Promise<void> {
  return BackgroundInbox.start({ baseUrl: getApiUrl(), token: getApiToken(), clientId: inboxClientId().slice(0, 61) });
}

export type BgNotifyResult = 'ok' | 'denied' | 'error';

/** Переключатель в «Настройки» → «Подключение»: включение спрашивает
 * разрешение на уведомления (Android 13+) и запускает службу. */
export async function setBgNotify(on: boolean): Promise<BgNotifyResult> {
  if (!isNativeApp()) return 'error';
  if (!on) {
    rememberEnabled(false);
    await BackgroundInbox.stop().catch(() => {});
    return 'ok';
  }
  try {
    const { granted } = await BackgroundInbox.requestNotificationPermission();
    if (!granted) return 'denied';
    await startService();
    rememberEnabled(true);
    return 'ok';
  } catch {
    return 'error';
  }
}

/** Запуск приложения (ядро ответило): включено — служба перезапускается со
 * свежими адресом и токеном; канал VPC Link — служба не нужна. Уведомления
 * запрещены в настройках Android — служба тоже не нужна: показать она ничего
 * не сможет, а курсор на ядре сдвинет (сообщения пропадут для шторки).
 * Выбор «включено» остаётся — в настройках подсказка, как разрешить. */
export function syncBgInbox() {
  if (!isNativeApp() || !bgNotifyEnabled()) return;
  if (isLinkMode() || !getApiUrl()) {
    void BackgroundInbox.stop().catch(() => {});
    return;
  }
  void BackgroundInbox.status()
    .then((s) => (s.notificationsEnabled === false ? BackgroundInbox.stop() : startService()))
    .catch(() => {});
}

/** Включено, но уведомления запрещены: снова спросить разрешение (Android 13+
 * покажет системный запрос, если его не отклонили насовсем) и, если дали,
 * запустить службу. */
export async function retryBgNotifyPermission(): Promise<BgNotifyResult> {
  if (!isNativeApp()) return 'error';
  try {
    const { granted } = await BackgroundInbox.requestNotificationPermission();
    if (!granted) return 'denied';
    await startService();
    return 'ok';
  } catch {
    return 'error';
  }
}

/** Смена сервера или отвязка: служба не должна опрашивать прежнее ядро
 * (выбор «включено» остаётся — с новым адресом служба запустится снова). */
export function stopBgInbox() {
  if (!isNativeApp()) return;
  void BackgroundInbox.stop().catch(() => {});
}

export function bgInboxStatus() {
  return BackgroundInbox.status();
}

/** Тап уведомления → чат персоны. Вызывать после того, как App подписался
 * на OPEN_CHAT_EVENT: тап, которым приложение запустили, ждёт в плагине. */
export function listenBgNotificationTaps(): () => void {
  if (!isNativeApp()) return () => {};
  const open = (persona: string | null | undefined) => {
    if (persona) window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: persona }));
  };
  let handle: PluginListenerHandle | null = null;
  let closed = false;
  // Сначала подписка, потом отложенный тап: иначе тап между ними потеряется
  void BackgroundInbox.addListener('notificationTap', (d) => open(d.persona))
    .then((h) => {
      if (closed) {
        void h.remove();
        return;
      }
      handle = h;
      return BackgroundInbox.getLaunchPersona().then((r) => open(r.persona));
    })
    .catch(() => {});
  return () => {
    closed = true;
    void handle?.remove();
  };
}

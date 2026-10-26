import { registerPlugin } from '@capacitor/core';
import type { PluginListenerHandle } from '@capacitor/core';
import { getApiToken, getApiUrl, inboxClientId, isLinkMode, isNativeApp } from './api';
import { OPEN_CHAT_EVENT, OPEN_ROOM_EVENT } from './notifications';
import { requestChatVoice } from './chatNavStore';

/* Приложение на Android: уведомления о сообщениях персон, когда приложение
   свёрнуто или закрыто. WebView в фоне не работает, поэтому опрос ядра
   (GET /api/inbox, долгий: ответ приходит, как только есть сообщение) делает
   нативная служба переднего плана
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
  testNotification(): Promise<{ shown: boolean; problem: 'denied' | 'channel' | null }>;
  // section: 'room' — тап по виджету комнаты, 'voice' — по виджету портрета
  // (голосовой режим чата), иначе — чат
  getLaunchPersona(): Promise<{ persona: string | null; section?: string | null }>;
  addListener(event: 'notificationTap', cb: (data: { persona: string; section?: string | null }) => void): Promise<PluginListenerHandle>;
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

export type NativeNotifyTest = 'ok' | 'denied' | 'channel' | 'error';

/** «Проверить» в настройках приложения: настоящее уведомление Android в
 * канал «Сообщения персон» — тем же путём, что сообщения персон (Notification
 * API веба в приложении не работает). Android 13+ сначала спросит разрешение. */
export async function testNativeNotification(): Promise<NativeNotifyTest> {
  if (!isNativeApp()) return 'error';
  try {
    await BackgroundInbox.requestNotificationPermission();
    const r = await BackgroundInbox.testNotification();
    if (r.shown) return 'ok';
    return r.problem === 'channel' ? 'channel' : 'denied';
  } catch {
    return 'error';
  }
}

/** Тап уведомления или виджета «Инициатива» → чат персоны, виджета комнаты →
 * её комната, виджета портрета → голосовой режим её чата. Вызывать после
 * того, как App подписался на OPEN_CHAT_EVENT и OPEN_ROOM_EVENT: тап,
 * которым приложение запустили, ждёт в плагине. */
export function listenBgNotificationTaps(): () => void {
  if (!isNativeApp()) return () => {};
  const open = (persona: string | null | undefined, section?: string | null) => {
    if (!persona) return;
    if (section === 'voice') requestChatVoice(persona);
    const event = section === 'room' ? OPEN_ROOM_EVENT : OPEN_CHAT_EVENT;
    window.dispatchEvent(new CustomEvent(event, { detail: persona }));
  };
  let handle: PluginListenerHandle | null = null;
  let closed = false;
  // Сначала подписка, потом отложенный тап: иначе тап между ними потеряется
  void BackgroundInbox.addListener('notificationTap', (d) => open(d.persona, d.section))
    .then((h) => {
      if (closed) {
        void h.remove();
        return;
      }
      handle = h;
      return BackgroundInbox.getLaunchPersona().then((r) => open(r.persona, r.section));
    })
    .catch(() => {});
  return () => {
    closed = true;
    void handle?.remove();
  };
}

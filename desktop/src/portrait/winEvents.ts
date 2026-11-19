import type { EventCallback, UnlistenFn } from '@tauri-apps/api/event';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';

/* События окна портрета. Окон портретов может быть несколько: портрет на
   столе и портреты персон разговора голосом (src-tauri/src/portrait.rs,
   гости). Панель шлёт каждому окну своё (emit_to по метке), а общий listen()
   из @tauri-apps/api/event ловит события всех окон — здесь только свои. */
export function listen<T>(event: string, handler: EventCallback<T>): Promise<UnlistenFn> {
  return getCurrentWebviewWindow().listen<T>(event, handler);
}

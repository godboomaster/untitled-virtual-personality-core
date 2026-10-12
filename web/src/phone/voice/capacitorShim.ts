/* Подмена @capacitor/core в бандле для голосовой службы (vite.phone-voice.config.ts).
   Служба телефона (WakeWordService.java) работает без WebView: разбор и решение
   «что сделать» — тот же actions.ts в JS-движке, а исполняет Java. Поэтому
   плагин здесь не исполняет, а записывает вызовы (ops) и сразу отвечает
   «получилось»; не получилось у Java — она сама поправит status итога.
   Список приложений даёт Java на каждый вызов (setApps). */

export interface VoiceOp {
  op: string;
  [arg: string]: unknown;
}

let ops: VoiceOp[] = [];
let apps: { label: string; package: string }[] = [];

export function setApps(list: { label: string; package: string }[]) {
  apps = Array.isArray(list) ? list : [];
}

export function takeOps(): VoiceOp[] {
  const out = ops;
  ops = [];
  return out;
}

export const Capacitor = {
  isPluginAvailable: (name: string) => name === 'PhoneActions',
  isNativePlatform: () => true,
  getPlatform: () => 'android',
};

export function registerPlugin<T>(name: string): T {
  return new Proxy(
    {},
    {
      get: (_, method: string) => (args: Record<string, unknown> = {}) => {
        if (name !== 'PhoneActions') return Promise.reject(Object.assign(new Error(`${name} нет в службе`), { code: 'unavailable' }));
        if (method === 'listApps') return Promise.resolve({ apps });
        ops.push({ op: method, ...args });
        return Promise.resolve({ handler: '' });
      },
    },
  ) as T;
}

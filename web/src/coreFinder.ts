import { registerPlugin } from '@capacitor/core';

/* Ядро в той же сети — плагин CoreFinder (Java, приложение на телефоне):
   discover — «кто тут ядро?» широковещательно (ответчик ядра —
   app/link/discovery.py): за секунду, на любом порту, с id и именем;
   find — перебор своей /24 по /api/health (ядра без ответчика). */

export interface DiscoveredCore {
  host: string; // адрес, с которого пришёл ответ
  id: string; // постоянный id ядра ('' — старое ядро)
  name: string;
  api: number; // 0 — API закрыто для сети (только VPC Link)
  token: boolean;
  link: number; // 0 — канала VPC Link нет
}

interface CoreFinderPlugin {
  discover(opts: { waitMs?: number; hosts?: string[] }): Promise<{ found: DiscoveredCore[]; nets: string[] }>;
  find(opts: { port: number }): Promise<{ found: string[]; nets: string[] }>;
}

let plugin: CoreFinderPlugin | undefined;

function finder(): CoreFinderPlugin {
  plugin ??= registerPlugin<CoreFinderPlugin>('CoreFinder');
  return plugin;
}

/** «Кто тут ядро?»; hosts — спросить и напрямую (сеть без широковещания).
 * Отказ с code no_wifi — телефон не в локальной сети. */
export function discoverCores(waitMs: number, hosts: string[] = []) {
  return finder().discover({ waitMs, hosts });
}

/** Перебор своей /24 на порту port. */
export function scanCores(port: number) {
  return finder().find({ port });
}

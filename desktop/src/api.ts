import { invoke } from '@tauri-apps/api/core';

// Типы — зеркало сериализации src-tauri/src (commands.rs, supervisor.rs, probe.rs)

export type Phase = 'stopped' | 'starting' | 'running' | 'stopping' | 'crashed' | 'external';

export interface ServiceView {
  phase: Phase;
  ours: boolean;
  pid: number | null;
  uptimeSec: number | null;
  responding: boolean;
  error: string | null;
  port: number;
  logFile: string;
}

export interface Pools {
  fromApi: boolean;
  h: { alive: boolean; mode: string | null; rescue: boolean };
  v: { alive: boolean; idleSec: number | null };
}

export interface Quarantine {
  site: string;
  kind: string;
  reason: string;
  leftSec: number;
}

export interface LogEntry {
  seq: number;
  time: string;
  level: string;
  msg: string;
}

export interface LogBatch {
  source: 'api' | 'raw' | 'none';
  reset: boolean;
  cursor: number;
  entries: LogEntry[];
}

export interface Snapshot {
  backend: ServiceView;
  web: ServiceView;
  busy: boolean;
  manageWeb: boolean;
  webUrl: string;
  pools: Pools;
  quarantine: Quarantine[];
  ollama: { up: boolean; models: { name: string; sizeGb: number }[] };
  memory: { freePercent: number; usedGb: number; totalGb: number; swapUsedGb: number };
  logs: LogBatch;
}

export interface Config {
  repoDir: string;
  python: string;
  node: string;
  manageWeb: boolean;
  webPort: number;
  startOnLaunch: boolean;
  autoRestart: boolean;
}

export interface Settings {
  config: Config;
  autostart: boolean;
  configPath: string;
  logsDir: string;
  repoOk: boolean;
}

export const api = {
  snapshot: (logCursor: number) => invoke<Snapshot>('snapshot', { logCursor }),
  start: () => invoke<void>('start'),
  stop: () => invoke<void>('stop'),
  restart: () => invoke<void>('restart'),
  rescue: (finish: boolean) => invoke<boolean>('rescue', { finish }),
  openWeb: () => invoke<void>('open_web'),
  openLogs: () => invoke<void>('open_logs'),
  getSettings: () => invoke<Settings>('get_settings'),
  saveSettings: (config: Config) => invoke<Settings>('save_settings', { config }),
  detectPython: (repoDir: string) => invoke<string>('detect_python', { repoDir }),
  setAutostart: (enabled: boolean) => invoke<boolean>('set_autostart', { enabled }),
  hidePanel: () => invoke<void>('hide_panel'),
  quit: () => invoke<void>('quit'),
};

export function isUp(phase: Phase): boolean {
  return phase === 'running' || phase === 'starting' || phase === 'external';
}

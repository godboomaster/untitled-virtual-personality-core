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

export type VoicePhase = 'off' | 'starting' | 'running' | 'stopping' | 'failed';

/* Состояние слушателя имени — WakeListener.status() (app/voice/wake_listener.py) */
export interface WakeStatus {
  running: boolean;
  state: string;
  persona: string | null;
  held: boolean;
  stage: string;
  names: string[];
  error: string;
  issue: string;
  lastHeard: string;
  /** Фильтр фона; нет — слушатель старее панели */
  near?: NearStatus | null;
  /** Языки распознавания (ru, en); нет — слушатель старее панели */
  langs?: string[];
  /** Язык разговора сейчас */
  lang?: string;
  /** Модель одного из языков не загрузилась — почему (слушает без неё) */
  langError?: string;
}

/** Фильтр фона «кто ближе к микрофону» (app/voice/near_gate.py) */
export interface NearStatus {
  on: boolean;
  /** Громкость вашего голоса, дБ; null — ещё не знает */
  level: number | null;
  /** panel — записали кнопкой, name — по фразе с именем */
  source: string;
  margin: number;
  calibrating: boolean;
  calibLeft: number;
  /** ok, no_speech */
  calibResult: string;
}

/** Выбор языков слушателя в панели */
export type WakeLang = 'ru,en' | 'ru' | 'en';

export interface VoiceView {
  enabled: boolean;
  lang: WakeLang;
  phase: VoicePhase;
  pid: number | null;
  status: WakeStatus | null;
  error: string | null;
  logFile: string;
}

/** Портрет на рабочем столе: на столе ли и чей */
export interface PortraitBrief {
  shown: boolean;
  persona: string;
  /** Клавиши анимаций: off — привязок нет, on — слушает, mouse — только
   *  мышь (нет доступа «Мониторинг ввода»), unsupported — не умеет */
  keys?: 'off' | 'on' | 'mouse' | 'unsupported';
}

export interface PortraitInfo extends PortraitBrief {
  personas: { id: string; name: string }[];
  /** Бот не отвечает: список и атласы — из папки данных ядра */
  fromDisk: boolean;
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
  voice: VoiceView;
  portrait: PortraitBrief;
  /** Отсутствует в обычной панели; контроллер создаётся только с --robot. */
  robot?: RobotView;
}

export type RobotAction = 'enable' | 'project' | 'stop' | 'poweroff';

export interface RobotConfig {
  host: string;
  user: string;
  port: number;
  identityFile: string;
  deviceId: string;
}

export interface RobotIdleSettings {
  intervalMinutes: number;
  probabilityPercent: number;
}

export interface RobotView {
  config: RobotConfig;
  service: 'unknown' | 'active' | 'inactive' | 'failed' | 'unreachable';
  connected: boolean | null;
  busy: boolean;
  action: RobotAction | null;
  error: string | null;
  sshError: string | null;
  shutdownRequested: boolean;
  shutdownStartedAt: number | null;
  powerState: 'unknown' | 'on' | 'shutting_down' | 'off';
  idleSettings: RobotIdleSettings | null;
  idleError: string | null;
}

export interface Config {
  repoDir: string;
  python: string;
  node: string;
  manageWeb: boolean;
  webPort: number;
  startOnLaunch: boolean;
  autoRestart: boolean;
  /** null — как WAKE_LISTEN в .env; переключается кнопкой и в трее */
  wakeListen: boolean | null;
  /** null — как WAKE_LANG в .env; выбирается в «Голосовой активации» */
  wakeLang: string | null;
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
  setWake: (enabled: boolean) => invoke<VoiceView>('set_wake', { enabled }),
  setWakeLang: (lang: WakeLang) => invoke<VoiceView>('set_wake_lang', { lang }),
  wakeCalibrate: () => invoke<VoiceView>('wake_calibrate'),
  wakeForgetVoice: () => invoke<VoiceView>('wake_forget_voice'),
  portraitInfo: () => invoke<PortraitInfo>('portrait_info'),
  setPortrait: (shown: boolean, persona?: string) => invoke<void>('portrait_set', { shown, persona }),
  keysSettings: () => invoke<void>('portrait_keys_settings'),
  hidePanel: () => invoke<void>('hide_panel'),
  quit: () => invoke<void>('quit'),
  robotAction: (action: RobotAction) => invoke<void>('robot_action', { action }),
  robotSave: (config: RobotConfig) => invoke<RobotView>('robot_save', { config }),
  robotIdleSave: (settings: RobotIdleSettings) => invoke<RobotView>('robot_idle_save', { settings }),
};

export function isUp(phase: Phase): boolean {
  return phase === 'running' || phase === 'starting' || phase === 'external';
}

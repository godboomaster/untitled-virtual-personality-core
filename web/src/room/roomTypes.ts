/* Типы API комнаты (контракт GET/PUT /api/personas/{p}/room*, см. спеку
   «Room presence»). Общие для раздела «Комната», PiP-окна и арт-мастерской. */

import type { InventoryEntry, LivingFeedEntry, LivingMood } from '../api';
import type { RoomAvatarPreset } from '../mockData';

// Встроенные места комнаты; места предметов — `item:<название>`
export type RoomBuiltinSpot = 'desk' | 'window' | 'shelf' | 'bed' | 'chair' | 'floor' | 'away';
export type RoomSpotKey = RoomBuiltinSpot | `item:${string}`;

// Позы с сервера; with_you и glance — только клиентские (кратковременные)
export type RoomPose = 'stand' | 'sit' | 'read' | 'write' | 'look' | 'sleep' | 'away';
export type RoomClientPose = RoomPose | 'with_you' | 'glance';

// Зоны, куда LLM ставит новые предметы
export type RoomZone = 'desk' | 'shelf' | 'window' | 'floor' | 'wall' | 'bed';

// Место в комнате (config.spots): встроенное или вокруг предмета
export interface RoomSpot {
  key: string; // RoomSpotKey
  place: string; // «за столом»
  label: string; // «пишет в дневник»
  pose: RoomPose;
  item: string | null; // название предмета для item:-мест
  keywords?: string[]; // из YAML room.spots (если бэкенд отдаёт)
}

// Описание места, придуманное вокруг предмета (placements / layout)
export interface RoomItemSpot {
  label: string;
  place: string;
  pose: RoomPose;
}

export interface RoomViewConfig {
  props: string[]; // RoomPropId
  pet: 'cat' | 'crow' | 'none';
  pet_label?: string | null;
  poster_label?: string | null;
  spots: RoomSpot[];
  is_default: boolean;
}

// Откуда взято состояние: веб-чат или Telegram-чат персоны
export interface RoomSource {
  context: string; // api_<persona> | <persona>
  chat_id: string;
  kind: 'web' | 'telegram';
  last_activity: number | null; // epoch, сек
}

export interface RoomLivingState {
  energy: number;
  mood: LivingMood;
  pastime: string;
  location: string;
  spot?: string | null; // старый бэкенд без комнаты — поля нет
  pose?: RoomPose | null;
  pastime_since?: number | null; // epoch, сек
  last_tick_at?: number;
  updated_at?: string;
  internal_note?: string;
}

export interface RoomPlan {
  title: string;
  due?: string | number | null;
  [key: string]: unknown;
}

export interface RoomLiving {
  enabled: boolean;
  ui_sync: boolean;
  state: RoomLivingState | null;
  recent_events: LivingFeedEntry[];
  plans?: RoomPlan[];
}

export interface RoomPlacement {
  zone: RoomZone | null; // null — LLM решила не ставить предмет на виду
  spot: RoomItemSpot | null;
  placed_by: 'llm' | 'heuristic';
  at: number;
}

export interface RoomFocus {
  active: boolean;
  started_at: number | null;
  minutes: number | null;
}

// GET /api/personas/{p}/room
export interface RoomView {
  source: RoomSource;
  config: RoomViewConfig;
  living: RoomLiving | null;
  inventory: InventoryEntry[];
  placements: Record<string, RoomPlacement>;
  focus: RoomFocus;
}

// layout.json: пользовательские правки предметов и аватара
export interface RoomLayoutItem {
  marker?: { x: number; y: number } | null;
  size?: number; // 2..25, % ширины сцены
  icon?: string | null;
  image?: string | null; // dataURL
  spot?: RoomItemSpot | null;
  hidden?: boolean;
}

export interface RoomLayout {
  items: Record<string, RoomLayoutItem>;
  avatar: RoomAvatarPreset | null;
  updated_at?: string;
}

// PUT /room/layout: частичный патч; null у предмета — удалить запись
export interface RoomLayoutPatch {
  items?: Record<string, Partial<RoomLayoutItem> | null>;
  avatar?: RoomAvatarPreset | null;
}

export interface RoomStyle {
  description: string;
  reference: string | null;
  updated_at?: string;
}

export interface RoomArtAsset {
  dataUrl: string;
  anchor: { x: number; y: number };
}

export interface RoomArtBg {
  dataUrl: string;
  floorPoints: Record<string, { x: number; y: number }>;
}

// art.json: спрайт (базовый + по позам) и фон комнаты
export interface RoomArtData {
  sprite: RoomArtAsset | null;
  sprites?: Record<string, RoomArtAsset>;
  room_bg: RoomArtBg | null;
}

export interface RoomArtPatch {
  sprite?: RoomArtAsset | null;
  sprites?: Record<string, RoomArtAsset | null> | null;
  room_bg?: RoomArtBg | null;
}

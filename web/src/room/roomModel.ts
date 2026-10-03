/* Чистая модель комнаты: конфиг (бэкенд → мок персоны → дефолт), точки мест
   на процедурной и фоновой сцене, авто-метки предметов по зонам, вывод места
   из текста занятия (старый бэкенд), демо-расписание и «уже N мин».
   Без React и без глобалов document/window — годится и для PiP-окна. */

import type { InventoryEntry } from '../api';
import type { InventoryItem, InventoryTag, RoomAvatarPreset, RoomConfig, RoomPropId } from '../mockData';
import type {
  RoomBuiltinSpot,
  RoomClientPose,
  RoomItemSpot,
  RoomLayoutItem,
  RoomLivingState,
  RoomPlacement,
  RoomPose,
  RoomView,
  RoomZone,
} from './roomTypes';

type T = (key: string, vars?: Record<string, string | number>) => string;

export interface Point {
  x: number; // доля ширины сцены (0..1)
  y: number; // доля высоты сцены (0..1), точка контакта с полом
  // Процедурная сцена: точная привязка к реквизиту, стоящему в px
  // (left: calc(bx·100% + ox px)); x/y тогда — приближение для эталонной
  // сцены 790×430 (для тех, кому нужны доли, — например, скинов)
  pin?: { bx: number; ox: number; by: number; oy: number };
}

export interface SceneSpot {
  key: string;
  place: string;
  label: string;
  pose: RoomPose;
  item: string | null;
  keywords?: string[];
}

// Итоговый конфиг сцены, откуда бы он ни пришёл
export interface SceneConfig {
  props: RoomPropId[];
  pet: 'cat' | 'crow' | 'none';
  petLabel?: string;
  posterLabel?: string;
  spots: SceneSpot[];
  source: 'backend' | 'mock' | 'default';
  // Моки: пресет аватара (внешний вид; данные телеметрии из моков не берутся)
  avatar?: RoomAvatarPreset;
}

// ── Хэш и детерминированный «шум» ──

// FNV-1a: стабильный uint32 по строке (джиттер меток, задержки моргания)
export function hashStr(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

// Доля 0..1 из хэша строки (соль разводит оси)
export function hashFrac(s: string, salt = ''): number {
  return (hashStr(salt + '|' + s) % 10007) / 10007;
}

// Стабильный числовой id предмета по названию (для InventoryItem.id)
export function itemIdFor(name: string): number {
  return (hashStr(name.toLowerCase()) % 1_000_000_000) + 1;
}

// ── Конфиг ──

const KNOWN_PROPS = new Set<RoomPropId>([
  'rug', 'garland', 'clock', 'poster', 'curtains',
  'shelf', 'shelfLower', 'desk', 'chair', 'bed', 'lamp', 'plant',
  'easel', 'frames', 'brushJars', 'mirror', 'secondMirror',
  'candles', 'scrolls', 'serverRack', 'masterTerminal',
]);

export const DEFAULT_ROOM_PROPS: RoomPropId[] = ['rug', 'clock', 'curtains', 'shelf', 'desk', 'chair', 'bed', 'lamp', 'plant'];

const SERVER_POSES = new Set<RoomPose>(['stand', 'sit', 'read', 'write', 'look', 'sleep', 'away']);

export const BUILTIN_POSE: Record<RoomBuiltinSpot, RoomPose> = {
  desk: 'write',
  window: 'look',
  shelf: 'read',
  bed: 'sleep',
  chair: 'sit',
  floor: 'sit',
  away: 'away',
};

export function isServerPose(p: unknown): p is RoomPose {
  return typeof p === 'string' && SERVER_POSES.has(p as RoomPose);
}

// Встроенное место с подписями из словаря (дефолтная комната, моки)
function builtinSpot(key: RoomBuiltinSpot, t: T): SceneSpot {
  return {
    key,
    place: t(`room.spot.${key}.place`),
    label: t(`room.spot.${key}.label`),
    pose: BUILTIN_POSE[key],
    item: null,
  };
}

// Дефолтный аватар — детерминированно по id, чтобы у персон без мока он различался
export function defaultAvatarFor(personaId: string): RoomAvatarPreset {
  const h = hashStr(personaId);
  const heads: RoomAvatarPreset['head'][] = ['circle', 'square', 'hex', 'diamond'];
  return { head: heads[h % 4], eyes: (h >>> 3) % 5, accessory: (h >>> 7) % 4, shade: (h >>> 11) % 4 };
}

// Конфиг сцены: бэкенд (онлайн, GET /room) → мок этой персоны → общий дефолт.
// Комнату Коннора другим персонам не подставляем
export function resolveRoomConfig(args: {
  view: RoomView | null;
  live: boolean;
  mock: RoomConfig | undefined;
  t: T;
}): SceneConfig {
  const { view, live, mock, t } = args;
  if (live && view?.config) {
    const c = view.config;
    const props = c.props.filter((p): p is RoomPropId => KNOWN_PROPS.has(p as RoomPropId));
    const spots: SceneSpot[] = c.spots
      .filter((s) => s.key !== 'away')
      .map((s) => ({
        key: s.key,
        place: s.place,
        label: s.label,
        pose: isServerPose(s.pose) ? s.pose : poseForKey(s.key),
        item: s.item ?? null,
        ...(s.keywords ? { keywords: s.keywords } : {}),
      }));
    return {
      props: props.length ? props : DEFAULT_ROOM_PROPS,
      pet: c.pet === 'cat' || c.pet === 'crow' ? c.pet : 'none',
      petLabel: c.pet_label ?? undefined,
      posterLabel: c.poster_label ?? undefined,
      spots: spots.length ? spots : defaultSpots(t),
      source: 'backend',
      ...(mock ? { avatar: mock.avatar } : {}),
    };
  }
  if (mock) {
    const poseByKey: Record<string, RoomPose> = { desk: 'write', window: 'look', shelf: 'read' };
    const spots: SceneSpot[] = mock.pastimes.map((p) => ({
      key: p.key,
      place: p.place,
      label: p.label,
      pose: poseByKey[p.key] ?? 'stand',
      item: null,
    }));
    if (mock.props.includes('bed')) spots.push(builtinSpot('bed', t));
    return {
      props: mock.props,
      pet: mock.pet,
      petLabel: mock.petLabel,
      posterLabel: mock.posterLabel,
      spots,
      source: 'mock',
      avatar: mock.avatar,
    };
  }
  return {
    props: DEFAULT_ROOM_PROPS,
    pet: 'none',
    spots: defaultSpots(t),
    source: 'default',
  };
}

function defaultSpots(t: T): SceneSpot[] {
  return (['desk', 'window', 'shelf', 'bed'] as const).map((k) => builtinSpot(k, t));
}

function poseForKey(key: string): RoomPose {
  return (BUILTIN_POSE as Record<string, RoomPose>)[key] ?? 'stand';
}

// Места предметов (item:<название>) добавляются к конфигу, если бэкенд их не
// прислал: офлайн-предметы с флагом spot и меткой
export function withItemSpots(cfg: SceneConfig, items: InventoryItem[], t: T): SceneConfig {
  const extra: SceneSpot[] = [];
  for (const i of items) {
    if (!i.spot || (!i.marker && !i.zone)) continue;
    const key = `item:${i.name}`;
    if (cfg.spots.some((s) => s.key === key)) continue;
    extra.push({
      key,
      place: i.spotInfo?.place || i.name,
      label: i.spotInfo?.label || t('room.itemPastime', { name: i.name }),
      pose: i.spotInfo?.pose && isServerPose(i.spotInfo.pose) ? i.spotInfo.pose : 'stand',
      item: i.name,
    });
  }
  return extra.length ? { ...cfg, spots: [...cfg.spots, ...extra] } : cfg;
}

// ── Точки мест ──

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

// Эталонная сцена раздела (для приближённых долей у точек с привязкой в px)
const REF_W = 790;
const REF_H = 430;

// Точка с привязкой к реквизиту: bx/by — доли сцены, ox/oy — сдвиг в px
function pinned(bx: number, ox: number, by: number, oy: number): Point {
  return {
    x: clamp(bx + ox / REF_W, 0, 1),
    y: clamp(by + oy / REF_H, 0, 1),
    pin: { bx, ox, by, oy },
  };
}

// CSS-позиция точки (left/top); calc — и для долей, чтобы переход между
// точками разного вида шёл плавно
export function pointStyle(p: Point): { left: string; top: string } {
  const { bx, ox, by, oy } = p.pin ?? { bx: p.x, ox: 0, by: p.y, oy: 0 };
  return { left: `calc(${bx * 100}% + ${ox}px)`, top: `calc(${by * 100}% + ${oy}px)` };
}

// Процедурная сцена (index.css): линия стены и пола — 30% снизу (FLOOR),
// столешница стола лежит на ней (left: 9%, 130px), ножки уходят вниз;
// полка — top 70px, left 30px; окно — right 30px, 104px; кровать — right 5%,
// 140px, верх матраса на 26px выше линии; ковёр — 28..70% по центру.
// Точка — «ступни» фигуры (у лежащей — верх матраса, см. SLEEP_ANCHOR)
const FLOOR = 0.7;
const SEAT_OY = 20; // за столом: ступни за столешницей, стол закрывает пояс
const PROCEDURAL_SPOT_POINTS: Record<Exclude<RoomBuiltinSpot, 'away'>, Point> = {
  desk: pinned(0.09, 88, FLOOR, SEAT_OY),
  chair: pinned(0.09, 88, FLOOR, SEAT_OY),
  shelf: pinned(0, 76, FLOOR, 62), // под полкой, перед левым краем стола
  window: pinned(1, -100, FLOOR, 40), // под окном, перед кроватью
  bed: pinned(0.95, -70, FLOOR, -26), // верх матраса
  floor: pinned(0.49, 0, 0.9, 0), // на ковре
};

// Место «полки» у персон с другим главным реквизитом (моки/YAML: «у
// мольберта», «у зеркала», «у серверной») — рядом с тем, что нарисовано
function featurePoint(props: RoomPropId[]): Point {
  if (props.includes('easel')) return pinned(0.31, 96, FLOOR, 26);
  if (props.includes('mirror')) return pinned(0.38, 60, FLOOR, 24);
  if (props.includes('serverRack')) return pinned(0, 70, FLOOR, 60);
  if (props.includes('shelf')) return PROCEDURAL_SPOT_POINTS.shelf;
  return pinned(0.5, 0, FLOOR, 40);
}

// Позы, в которых фигура сидит
const SEATED = new Set<string>(['sit', 'write', 'read']);

// Зоны для предметов без пользовательской метки: диапазон внутри —
// детерминированный джиттер по названию; ox — px от bx (реквизит в px)
const PROCEDURAL_ZONES: Record<RoomZone, { bx: number; x: [number, number]; ox?: [number, number]; by: number; y: [number, number]; oy?: number }> = {
  desk: { bx: 0.09, x: [0, 0], ox: [12, 56], by: FLOOR, y: [0, 0], oy: -7 },
  shelf: { bx: 0, x: [0, 0], ox: [40, 110], by: 0, y: [0, 0], oy: 70 },
  window: { bx: 1, x: [0, 0], ox: [-124, -44], by: 0, y: [0, 0], oy: 142 },
  floor: { bx: 0, x: [0.36, 0.62], by: 0, y: [0.86, 0.93] },
  wall: { bx: 0, x: [0.4, 0.56], by: 0, y: [0.22, 0.34] },
  bed: { bx: 0.95, x: [0, 0], ox: [-104, -30], by: FLOOR, y: [0, 0], oy: -26 },
};

type FloorPoints = Partial<Record<string, Point>>;

// Точка места: предметное место → метка предмета; фон → откалиброванные
// точки пола (нет ключа — стол); процедурная сцена → у нарисованного
// реквизита. pose уточняет кровать: спит — на матрасе, сидит — на краю,
// иначе стоит перед ней. null — персоны нет в комнате (away)
export function spotPoint(args: {
  spotKey: string;
  config: SceneConfig;
  floorPoints?: FloorPoints | null;
  items?: InventoryItem[];
  pose?: string;
}): Point | null {
  const { spotKey, config, floorPoints, items, pose } = args;
  if (spotKey === 'away') return null;
  if (spotKey.startsWith('item:')) {
    const name = spotKey.slice(5).toLowerCase();
    const item = items?.find((i) => i.name.toLowerCase() === name);
    const p = item ? itemPoint(item, floorPoints) : null;
    if (p) {
      if (floorPoints) return { x: p.x, y: Math.min(0.95, p.y) };
      // Предмет выше пола (стол, полка, окно) — фигура стоит на полу под ним
      return p.y >= FLOOR + 0.04 ? p : pinned(p.pin?.bx ?? p.x, p.pin?.ox ?? 0, FLOOR, 34);
    }
    return spotPoint({ spotKey: 'desk', config, floorPoints, items, pose });
  }
  if (floorPoints) {
    const fp = floorPoints[spotKey] ?? floorPoints.desk ?? { x: 0.5, y: 0.8 };
    // Точка кровати на фоне — у пола; спящий лежит на матрасе, чуть выше
    return spotKey === 'bed' && pose === 'sleep' && floorPoints.bed ? pinned(fp.x, 0, fp.y, -26) : fp;
  }
  if (spotKey === 'shelf') return featurePoint(config.props);
  if (spotKey === 'bed') {
    if (!config.props.includes('bed')) return PROCEDURAL_SPOT_POINTS.floor;
    if (pose && pose !== 'sleep') return SEATED.has(pose) ? pinned(0.95, -70, FLOOR, -12) : pinned(0.95, -70, FLOOR, 40);
  }
  return (PROCEDURAL_SPOT_POINTS as Record<string, Point>)[spotKey] ?? PROCEDURAL_SPOT_POINTS.desk;
}

// Точка предмета на сцене: пользовательская метка или авто-метка по зоне
export function itemPoint(item: InventoryItem, floorPoints?: FloorPoints | null): Point | null {
  if (item.marker) return item.marker;
  if (!item.zone) return null;
  return zonePoint(item.zone as RoomZone, item.name, floorPoints);
}

export function zonePoint(zone: RoomZone, name: string, floorPoints?: FloorPoints | null): Point {
  const jx = hashFrac(name, 'x');
  const jy = hashFrac(name, 'y');
  if (floorPoints) {
    // Фоновая сцена: зона привязана к откалиброванной точке пола (стены и
    // полки не калибруются — поднимаем точку над полом)
    const base = floorPoints[zone] ?? floorPoints.desk ?? { x: 0.5, y: 0.8 };
    const lift = zone === 'wall' || zone === 'shelf' ? 0.3 : zone === 'window' ? 0.2 : zone === 'desk' ? 0.08 : 0;
    return {
      x: clamp(base.x + (jx - 0.5) * 0.1, 0.03, 0.97),
      y: clamp(base.y - lift + (jy - 0.5) * 0.04, 0.05, 0.97),
    };
  }
  const a = PROCEDURAL_ZONES[zone] ?? PROCEDURAL_ZONES.floor;
  const lerp = (r: [number, number], f: number) => r[0] + (r[1] - r[0]) * f;
  return pinned(a.bx + lerp(a.x, jx), a.ox ? Math.round(lerp(a.ox, jx)) : 0, a.by + lerp(a.y, jy), a.oy ?? 0);
}

// ── Место и поза из состояния ──

// Ключевые слова встроенных мест (ru + en); порядок проверки — от частных к общим
const BUILTIN_KEYWORDS: [RoomBuiltinSpot, string[]][] = [
  ['bed', ['спит', 'сон', 'спать', 'засып', 'кроват', 'лёг', 'лег ', 'дрем', 'sleep', 'bed', 'nap']],
  ['window', ['окн', 'дожд', 'улиц', 'закат', 'рассвет', 'window', 'rain', 'street', 'sunset']],
  ['shelf', ['книг', 'полк', 'чита', 'book', 'shelf', 'read']],
  ['chair', ['кресл', 'сидит', 'armchair', 'sitting']],
  ['floor', ['на полу', 'ковр', 'ковёр', 'floor', 'rug']],
  ['desk', ['стол', 'пиш', 'дневник', 'записыв', 'рису', 'работа', 'desk', 'writ', 'diary', 'journal', 'draw']],
  ['away', ['ушёл', 'ушел', 'ушла', 'вышел', 'вышла', 'нет дома', 'гуля', 'магазин', 'away', 'went out', 'outside']],
];

// Место из состояния: валидный state.spot → он; иначе ключевые слова по
// тексту занятия/локации (старый бэкенд без spot) → стол
export function inferSpot(state: RoomLivingState | null | undefined, spots: SceneSpot[]): string {
  const keys = new Set(spots.map((s) => s.key));
  if (state?.spot && (state.spot === 'away' || keys.has(state.spot))) return state.spot;
  const text = `${state?.pastime ?? ''} ${state?.location ?? ''}`.toLowerCase();
  if (text.trim()) {
    for (const s of spots) {
      if (s.item && text.includes(s.item.toLowerCase())) return s.key;
    }
    for (const s of spots) {
      if (s.keywords?.some((k) => k && text.includes(k.toLowerCase()))) return s.key;
    }
    for (const [key, words] of BUILTIN_KEYWORDS) {
      if (key !== 'away' && !keys.has(key)) continue;
      if (words.some((w) => text.includes(w))) return key;
    }
  }
  if (keys.has('desk')) return 'desk';
  return spots[0]?.key ?? 'desk';
}

export function inferPose(state: RoomLivingState | null | undefined, spotKey: string, spots: SceneSpot[]): RoomPose {
  if (spotKey === 'away') return 'away';
  if (state?.pose && isServerPose(state.pose)) return state.pose;
  return spots.find((s) => s.key === spotKey)?.pose ?? poseForKey(spotKey);
}

// ── Длительность ──

// «уже 40 мин» по pastime_since (epoch, сек); null — неизвестно
export function durationSince(sinceSec: number | null | undefined, nowMs: number, t: T): string | null {
  if (!sinceSec || !Number.isFinite(sinceSec)) return null;
  const min = Math.max(0, Math.floor((nowMs / 1000 - sinceSec) / 60));
  if (min < 1) return t('room.dur.justNow');
  if (min < 60) return t('room.dur.min', { n: min });
  const h = Math.floor(min / 60);
  if (h < 24) {
    const m = min % 60;
    return m ? t('room.dur.hMin', { h, m }) : t('room.dur.h', { h });
  }
  return t('room.dur.d', { n: Math.floor(h / 24) });
}

// ── Предметы: инвентарь бэкенда + раскладка + размещение LLM ──

// Тег карточки по источнику предмета
export function entryTag(e: InventoryEntry): InventoryTag {
  if (e.tags?.includes('created')) return 'created';
  if (e.source === 'living_event' || e.source === 'found') return 'found';
  return 'gift';
}

// Иконка по умолчанию — по ключевым словам названия (пользователь может сменить)
function guessIcon(name: string): string {
  const n = name.toLowerCase();
  if (/книг|book|свит|журнал|блокнот|notebook/.test(n)) return 'book';
  if (/круж|чаш|cup|mug|банк/.test(n)) return 'cup';
  if (/карт|card/.test(n)) return 'cards';
  if (/фото|photo|открыт|снимок/.test(n)) return 'photo';
  if (/карандаш|ручк|перо|кист|pen|brush/.test(n)) return 'pencil';
  if (/холст|рам|картин|frame|canvas/.test(n)) return 'frame';
  if (/диск|пластин|disc|record/.test(n)) return 'disc';
  if (/кабел|провод|cable/.test(n)) return 'cable';
  return 'gem';
}

export function buildOnlineItems(args: {
  inventory: InventoryEntry[];
  layout: Record<string, RoomLayoutItem>;
  placements: Record<string, RoomPlacement>;
}): InventoryItem[] {
  const { inventory, layout, placements } = args;
  const out: InventoryItem[] = [];
  for (const e of inventory) {
    const lay = layout[e.name] ?? {};
    if (lay.hidden) continue;
    const pl = placements[e.name];
    const spotInfo: RoomItemSpot | null = lay.spot !== undefined ? lay.spot : (pl?.spot ?? null);
    out.push({
      id: itemIdFor(e.name),
      icon: lay.icon || guessIcon(e.name),
      name: e.name,
      description: e.description,
      tag: entryTag(e),
      ...(lay.image ? { image: lay.image } : {}),
      ...(lay.marker ? { marker: lay.marker } : {}),
      ...(lay.size != null ? { size: lay.size } : {}),
      spot: !!spotInfo,
      spotInfo,
      zone: pl?.zone ?? null,
      acquired: e.acquired,
    });
  }
  return out;
}

// Предмет получен сегодня (acquired — дата YYYY-MM-DD в поясе бэкенда)
export function acquiredToday(acquired: string | undefined, nowMs: number): boolean {
  if (!acquired) return false;
  const d = new Date(nowMs);
  const pad = (n: number) => String(n).padStart(2, '0');
  return acquired.slice(0, 10) === `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Итоговая поза для рисования: клиентские реплики поверх серверной
export function displayPose(serverPose: RoomPose, cue: RoomClientPose | null): RoomClientPose {
  if (serverPose === 'away') return 'away';
  // Спящего не будят ни взгляд при возвращении, ни «с тобой»
  if (serverPose === 'sleep') return 'sleep';
  if (cue === 'glance') return 'glance';
  // «С тобой» не отрывает от чтения/письма насовсем — просто поворачивается к зрителю
  if (cue === 'with_you') return 'with_you';
  return serverPose;
}

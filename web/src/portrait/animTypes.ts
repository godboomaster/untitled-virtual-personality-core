// Анимации портрета (app/api/portrait_anim.py): лист кадров rows×cols, который
// пользователь готовит сам, — ест, читает, идёт… Играются на портрете на
// рабочем столе: случайно (ротация) и на события

/** Когда играть. Порядок — порядок показа в карточке */
export type AnimTrigger =
  | 'idle' // ротация: случайно, когда свободна
  | 'walk' // шаги — цикл, пока идёт по экрану
  | 'llm' // персона выбирает сама (маркер [ACT:имя] в ответе)
  | 'pet' // погладили
  | 'hello' // вы вернулись к компьютеру
  | 'message' // персона написала первой (инициатива, напоминание)
  | 'think' // думает над ответом — цикл
  | 'drop' // бросили файл — читает, цикл до ответа
  | 'grab' // схватили — цикл, пока держат
  | 'fall' // падает — цикл
  | 'land' // приземлилась
  | 'jump' // прыжок, двойной клик
  | 'angry' // много тычков подряд
  | 'sleep' // засыпает
  | 'wake' // проснулась
  | 'break' // долго без перерыва
  | 'focus' // «посиди со мной» — цикл
  | 'mode'; // сменился режим персоны — переход в режим этого аватара (switch_from — из каких)

export const ANIM_TRIGGERS: readonly AnimTrigger[] = [
  'idle',
  'walk',
  'llm',
  'pet',
  'hello',
  'message',
  'think',
  'drop',
  'grab',
  'fall',
  'land',
  'jump',
  'angry',
  'sleep',
  'wake',
  'break',
  'focus',
  'mode',
];

/** Циклы: играют, пока длится состояние, а не loops раз */
export const LOOP_TRIGGERS: ReadonlySet<AnimTrigger> = new Set(['walk', 'think', 'drop', 'grab', 'fall', 'focus']);

/** Все поводы — циклы: анимация и так идёт по кругу, пока длится состояние */
export const alwaysLoops = (triggers: readonly AnimTrigger[]) => triggers.length > 0 && triggers.every((t) => LOOP_TRIGGERS.has(t));

export type AnimAlign = 'grid' | 'feet';
export type AnimFaces = 'none' | 'left' | 'right';

export interface PortraitAnim {
  id: string;
  name: string;
  rows: number;
  cols: number;
  frames: number; // играются первые frames кадров атласа по порядку чтения
  filled?: number; // клеток до последней непустой (кадров «авто»)
  slots?: number; // кадров в анимации (свой порядок: клетку листа можно взять не раз)
  atlas_rows?: number; // рядов в атласе: кадров больше, чем клеток листа, — больше rows
  order?: number[] | null; // какая клетка листа идёт кадром (редактор кадров)
  fps: number; // шаг всей анимации
  holds?: number[] | null; // сколько шагов стоит каждый кадр; нет — по одному
  loops: number; // сколько раз проиграть за раз
  loop?: boolean; // по кругу, пока не случится что-то ещё (useBody)
  scale: number; // высота кадра относительно стороны головы
  align: AnimAlign;
  bg: 'auto' | 'keep';
  tolerance: number;
  offset_x: number;
  offset_y: number;
  faces: AnimFaces; // куда идёт фигура в кадрах; в другую сторону — отражение (walkFor)
  triggers: AnimTrigger[];
  switch_from?: string[]; // переход (повод mode): из каких режимов ("" — обычный); пусто — из любого
  keys?: string[]; // клавиши и кнопки мыши, на которые играет (animKeys.ts)
  after?: string[]; // играет после любой из этих (id): та кончилась — эта
  before?: string[]; // играет перед любой из этих: сначала эта, потом та
  chance?: number; // в скольких процентах случаев связка срабатывает (100 — каждый раз)
  sound?: string | null; // MIME звука; нет — без звука
  sound_name?: string;
  sound_url?: string; // data-URL звука (приходит с атласом)
  sound_loop?: boolean; // повторять звук, пока анимация на экране
  volume?: number; // 0..100
  weight: number; // 1..10
  enabled: boolean;
  fw: number; // кадр в атласе, px
  fh: number;
  sink?: number; // пол кадра — на столько px выше его низа (фигура уходит под пол)
  ax?: number | null; // середина пола — столько px от левого края кадра (своя рамка); нет — посередине
  fix?: { floor?: number } | null; // правки «Кадров»; floor — низ экрана поднят над низом фигуры (что ниже — под краем)
  // Подглядывает из-за края экрана (не низа): линия в кадре атласа — px от
  // его левого края (left, right) или верхнего (top); что за ней — за краем
  edge?: { side: 'left' | 'right' | 'top'; at: number } | null;
  created_at?: number | null;
  updated_at?: number | null;
  atlas?: string; // data-URL webp: cols×rows кадров fw×fh
}

export interface AnimSettings {
  every_min: number; // средний интервал ротации, минут; 0 — ротации нет
  walk: boolean; // гуляет ли сама по экрану
  sleep_anim?: string | null; // выбранный цикл на всё время сна
}

// GET /api/personas/{p}/portrait/anims
export interface PortraitAnims {
  anims: PortraitAnim[];
  settings: AnimSettings;
  updated_at?: number | null;
}

// PUT /api/personas/{p}/portrait/anims/{id|new}: без data_url — переразметить загруженный лист
export interface AnimUpdate {
  data_url?: string;
  name: string;
  rows: number;
  cols: number;
  frames?: number | null;
  fps: number;
  loops: number;
  loop: boolean;
  scale: number;
  align: AnimAlign;
  bg: 'auto' | 'keep';
  tolerance: number;
  offset_x: number;
  offset_y: number;
  faces: AnimFaces;
  triggers: AnimTrigger[];
  switch_from?: string[];
  keys?: string[];
  after?: string[];
  before?: string[];
  chance?: number;
  /** Нет — звук как был; '' — убрать; data-URL — новый */
  sound_url?: string;
  sound_name?: string;
  sound_loop?: boolean;
  volume?: number;
  weight: number;
  enabled: boolean;
}

export const ANIM_SETTINGS_DEFAULT: AnimSettings = { every_min: 4, walk: true, sleep_anim: null };

/** Движения окна портрета: свои кадры на них — циклом, пока длится движение */
export type AnimMove = 'walk' | 'grab' | 'fall';
export const ANIM_MOVES: readonly AnimMove[] = ['walk', 'grab', 'fall'];

/** Новая анимация: поля по умолчанию */
export const NEW_ANIM: AnimUpdate = {
  name: '',
  rows: 4,
  cols: 4,
  frames: null,
  fps: 8,
  loops: 1,
  loop: false,
  scale: 1,
  align: 'grid',
  bg: 'auto',
  tolerance: 0,
  offset_x: 0,
  offset_y: 0,
  faces: 'none',
  triggers: ['idle'],
  keys: [],
  after: [],
  before: [],
  chance: 100,
  sound_loop: false,
  volume: 80,
  weight: 3,
  enabled: true,
};

/** Размер блока кадра на экране, px: длинная сторона — size × scale (size —
    сторона головы портрета), пропорции — как у клетки атласа */
/** Сетка атласа, что играют клиенты: cols кадров в ряд, рядов — по числу
 * кадров (кадров с повторами бывает больше, чем клеток листа rows×cols) */
export function animGrid(anim: Pick<PortraitAnim, 'rows' | 'cols' | 'atlas_rows'>): { rows: number; cols: number } {
  return { rows: Math.max(anim.rows, anim.atlas_rows ?? anim.rows), cols: anim.cols };
}

export function animBox(anim: PortraitAnim, size: number): { w: number; h: number } {
  const long = Math.max(1, size * anim.scale);
  const k = long / Math.max(anim.fw, anim.fh, 1);
  return { w: Math.round(anim.fw * k), h: Math.round(anim.fh * k) };
}

/** На сколько px (при той же size, что у animBox) низ кадра ниже пола:
    фигура в кадрах уходит под пол — блок ставят ниже низа головы на это */
export function animSink(anim: PortraitAnim, size: number): number {
  return ((anim.sink ?? 0) * animBox(anim, size).h) / Math.max(1, anim.fh);
}

/** Где середина пола кадра от его левого края, px при той же size: её
    ставят на середину головы (своя рамка кадра бывает несимметричной) */
export function animAnchor(anim: PortraitAnim, size: number): number {
  return ((anim.ax ?? anim.fw / 2) * animBox(anim, size).w) / Math.max(1, anim.fw);
}

/** Включённые анимации на событие */
export function animsFor(data: PortraitAnims | null | undefined, trigger: AnimTrigger): PortraitAnim[] {
  return (data?.anims ?? []).filter((a) => a.enabled && a.atlas && a.frames > 0 && a.triggers.includes(trigger));
}

/** Переходы в режим этого набора из режима from ("" — обычный): сначала
    те, где from назван, нет таких — «из любого» (switch_from пустой) */
export function switchAnimsFor(data: PortraitAnims | null | undefined, from: string): PortraitAnim[] {
  const all = animsFor(data, 'mode');
  const named = all.filter((a) => a.switch_from?.includes(from));
  return named.length ? named : all.filter((a) => !a.switch_from?.length);
}

/** Шаги в сторону dir (−1 влево, 1 вправо): сначала нарисованные в эту
    сторону, нет таких — в другую (mirror: отразить), нет и их — «на нас»
    (не отражаются). Шагов нет — null: портрет сам не гуляет */
export function walkFor(
  data: PortraitAnims | null | undefined,
  dir: number,
  rnd = Math.random,
): { anim: PortraitAnim; mirror: boolean } | null {
  const all = animsFor(data, 'walk');
  const order: AnimFaces[] = dir < 0 ? ['left', 'right', 'none'] : dir > 0 ? ['right', 'left', 'none'] : ['none', 'right', 'left'];
  for (const f of order) {
    const anim = pickWeighted(
      all.filter((a) => a.faces === f),
      rnd,
    );
    if (anim) return { anim, mirror: dir !== 0 && f !== 'none' && (f === 'left') !== dir < 0 };
  }
  return null;
}

/** Одна из анимаций по весам (weight); пусто — null */
export function pickWeighted<T extends { weight: number }>(items: T[], rnd = Math.random): T | null {
  const total = items.reduce((s, a) => s + Math.max(0, a.weight), 0);
  if (total <= 0) return null;
  let x = rnd() * total;
  for (const a of items) {
    x -= Math.max(0, a.weight);
    if (x < 0) return a;
  }
  return items[items.length - 1] ?? null;
}

// ── Связки: после и перед другими (anim.after, anim.before, anim.chance) ──

/** Анимаций в одной цепочке не больше; анимация в ней не повторяется */
export const CHAIN_MAX = 8;

/** Можно ли играть: включена, атлас есть, кадры есть */
export const animPlayable = (a: PortraitAnim) => a.enabled && !!a.atlas && a.frames > 0;

/** Сон — выбранный цикл, независимо от поводов и флага «по кругу». */
export function sleepAnimFor(data: PortraitAnims | null | undefined): PortraitAnim | null {
  return data?.anims.find((a) => a.id === data.settings.sleep_anim && animPlayable(a)) ?? null;
}
// Срабатывает ли связка этой анимации сейчас: chance процентов случаев
const rolls = (a: PortraitAnim, rnd: () => number) => rnd() * 100 < (a.chance ?? 100);
const oneOf = <T,>(items: T[], rnd: () => number): T | null => (items.length ? items[Math.floor(rnd() * items.length)] : null);

/** Что сыграть перед anim: анимации со связкой «перед» неё (и перед ними —
    свои), от первой к последней, без самой anim. seen — id уже в цепочке
    (дополняется). Несколько выпало — одна наугад */
export function leadIn(data: PortraitAnims | null | undefined, anim: PortraitAnim, seen: Set<string>, rnd = Math.random): PortraitAnim[] {
  const out: PortraitAnim[] = [];
  let cur = anim;
  seen.add(anim.id);
  while (seen.size < CHAIN_MAX) {
    const id = cur.id;
    const one = oneOf(
      (data?.anims ?? []).filter((a) => animPlayable(a) && !seen.has(a.id) && !!a.before?.includes(id) && rolls(a, rnd)),
      rnd,
    );
    if (!one) break;
    out.unshift(one);
    seen.add(one.id);
    cur = one;
  }
  return out;
}

/** Что сыграть, когда anim кончилась: анимация со связкой «после» неё (из
    выпавших по шансу — одна наугад), не из цепочки seen; нет — null */
export function followUp(data: PortraitAnims | null | undefined, anim: PortraitAnim, seen: Set<string>, rnd = Math.random): PortraitAnim | null {
  if (seen.size >= CHAIN_MAX) return null;
  return oneOf(
    (data?.anims ?? []).filter((a) => animPlayable(a) && !seen.has(a.id) && !!a.after?.includes(anim.id) && rolls(a, rnd)),
    rnd,
  );
}

/** Сколько секунд идёт один проход анимации (loops раз; кадры — с holds) */
export function animSeconds(anim: Pick<PortraitAnim, 'frames' | 'fps' | 'holds' | 'loops'>): number {
  let steps = 0;
  for (let i = 0; i < anim.frames; i++) steps += anim.holds?.[i] ?? 1;
  return (steps / Math.max(1, anim.fps)) * Math.max(1, anim.loops);
}

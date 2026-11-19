import type { RoomAvatarPreset } from '../mockData';

/* Тело аватара: вид и одежда (bodyOptions — индекс RoomAvatarPreset.body;
   нет поля — обычное). Рисует туловище с руками («верх») и ноги в позе
   фигуры — общее для комнаты (AvatarFigure, все позы) и бюста живой головы
   (portrait/LiveHead). Координаты — бокс фигуры 64×100 стоя (ступни — y=97).

   Одежда — слои поверх корпуса: торс другой тканью, рукава и штанины на
   долю длины, подол (платье, юбка, пальто, распахнутый халат), детали на
   груди и вещи на шее. Сидя подол укорачивается до колен — иначе закрыл бы
   ноги до пола. Скин красит корпус своей краской и может дать ткани свои
   цвета (FigurePaint.cloth); подложка со сдвигом — у корпуса и ткани. */

export const bodyOptions = [
  'base',
  'joints',
  'vest',
  'chibi',
  'hoodie',
  'suit',
  'shirt',
  'sweater',
  'overalls',
  'tee',
  'dress',
  'skirt',
  'coat',
  'labcoat',
  'vestbow',
  'spacesuit',
] as const;
export type BodyKind = (typeof bodyOptions)[number];

export function bodyKindOf(preset: RoomAvatarPreset): BodyKind {
  return bodyOptions[preset.body ?? 0] ?? 'base';
}

export type ClothKey = 'dark' | 'mid' | 'light' | 'white' | 'denim' | 'ink';
// Ткань по умолчанию — серая, как весь интерфейс
export const CLOTH_GRAY: Record<ClothKey, string> = {
  dark: '#2b2b2b',
  mid: '#7a7a7a',
  light: '#d4d4d4',
  white: '#ececec',
  denim: '#3c3f44',
  ink: '#5a5a5a',
};

/** Цвета тела: корпус и обводка — от оттенка или скина; back/shift — подложка скина */
export interface BodyColors {
  shell: string;
  line: string;
  eye: string;
  accent: string;
  cloth: Record<ClothKey, string>;
  back?: string;
  shift?: number;
}

/** Поза тела: повороты рук (°), доля длины руки, на чём сидит и насколько опущен верх */
export interface BodyPose {
  angles: [number, number];
  armFrac: number;
  seat: 'desk' | 'chair' | 'floor' | null;
  dy: number;
}
export const STAND_POSE: BodyPose = { angles: [6, -6], armFrac: 1, seat: null, dy: 0 };

type Paint = 'shell' | 'line' | 'eye' | 'accent' | 'none' | ClothKey;

interface Geom {
  top: number; // верх туловища — над ним голова с зазором
  torso: string;
  armX: [number, number];
  pivot: [number, number];
  armY: number;
  armW: number;
  armLen: number;
  armRx: number;
  legs: [number, number, number, number][];
  feet?: [number, number, number, number][];
}

const BASE: Geom = {
  top: 55,
  torso: 'M23,55 L41,55 L44,78 L20,78 Z',
  armX: [15, 44],
  pivot: [17.5, 46.5],
  armY: 57,
  armW: 5,
  armLen: 19,
  armRx: 0,
  legs: [
    [23, 78, 7, 19],
    [34, 78, 7, 19],
  ],
};

// Чиби: короткое туловище ниже, короткие ноги со ступнями; голова — ниже вместе с ним
const CHIBI: Geom = {
  top: 63,
  torso: 'M24,63 L40,63 L42,79 L22,79 Z',
  armX: [17.5, 41.5],
  pivot: [20, 44],
  armY: 64.5,
  armW: 5,
  armLen: 13,
  armRx: 2.5,
  legs: [
    [24.5, 79, 6.5, 14],
    [33, 79, 6.5, 14],
  ],
  feet: [
    [22.5, 92.5, 10, 4.5],
    [31, 92.5, 10, 4.5],
  ],
};

export function bodyTopOf(kind: BodyKind): number {
  return kind === 'chibi' ? CHIBI.top : BASE.top;
}

interface Part {
  d: string;
  fill?: Paint;
  stroke?: Paint;
  w?: number;
}
interface Outfit {
  torso?: Paint;
  sleeve?: [ClothKey, number]; // ткань и доля руки от плеча
  hands?: Paint; // перчатки
  pants?: [ClothKey, number]; // ткань и доля ноги от бедра
  boots?: ClothKey;
  skirt?: { c: ClothKey; from: number; hem: number; hw: number; open?: boolean };
  details?: Part[]; // на груди, поверх торса
  neck?: Part[]; // на шее (бабочка, горло, воротник)
  joints?: boolean;
}

const rr = (x: number, y: number, w: number, h: number, r = 0) =>
  r
    ? `M${x + r},${y} H${x + w - r} A${r},${r} 0 0 1 ${x + w},${y + r} V${y + h - r} A${r},${r} 0 0 1 ${x + w - r},${y + h} H${x + r} A${r},${r} 0 0 1 ${x},${y + h - r} V${y + r} A${r},${r} 0 0 1 ${x + r},${y} Z`
    : `M${x},${y} H${x + w} V${y + h} H${x} Z`;
const circ = (r: number, cx: number, cy: number) =>
  `M${cx - r},${cy} A${r},${r} 0 1 0 ${cx + r},${cy} A${r},${r} 0 1 0 ${cx - r},${cy} Z`;
const dots = (pts: [number, number][], r: number) => pts.map(([x, y]) => circ(r, x, y)).join(' ');

const OUTFITS: Record<BodyKind, Outfit> = {
  base: {},
  chibi: {},
  joints: { joints: true },
  vest: {
    details: [
      { d: 'M27,55 L32,63 L37,55', stroke: 'line', w: 1.2 },
      { d: dots([[32, 67.5], [32, 72.5]], 1), fill: 'eye' },
    ],
  },
  hoodie: {
    torso: 'mid',
    sleeve: ['mid', 15 / 19],
    pants: ['dark', 17 / 19],
    details: [
      { d: 'M27,70 H37 L38,76 H26 Z', stroke: 'ink', w: 1 },
      { d: 'M29.5,56 V62 M34.5,56 V62', stroke: 'light', w: 1 },
    ],
  },
  suit: {
    torso: 'dark',
    sleeve: ['dark', 16 / 19],
    pants: ['dark', 1],
    details: [
      { d: 'M28,55 L36,55 L32,64 Z', fill: 'light' },
      { d: 'M31,56 L33,56 L33.6,58 L32.8,68 L32,70 L31.2,68 L30.4,58 Z', fill: 'accent', stroke: 'ink', w: 0.6 },
      { d: 'M28,55 L31,66 M36,55 L33,66', stroke: 'line', w: 1 },
      { d: circ(0.9, 32, 73), fill: 'accent' },
    ],
  },
  shirt: {
    torso: 'light',
    sleeve: ['light', 11 / 19],
    pants: ['mid', 17 / 19],
    details: [
      { d: 'M27.5,55 L32,59 L36.5,55 L35,58.5 L32,60.5 L29,58.5 Z', fill: 'white', stroke: 'ink', w: 0.8 },
      { d: 'M32,60 V78', stroke: 'ink', w: 0.8 },
      { d: dots([[32, 64.5], [32, 69], [32, 73.5]], 0.7), fill: 'ink' },
    ],
  },
  sweater: {
    torso: 'mid',
    sleeve: ['mid', 16 / 19],
    pants: ['dark', 17 / 19],
    details: [
      { d: rr(20.4, 74.5, 23.2, 3.5), fill: 'dark', stroke: 'line', w: 1 },
      { d: 'M23,75 V78 M26,75 V78 M29,75 V78 M32,75 V78 M35,75 V78 M38,75 V78 M41,75 V78', stroke: 'line', w: 0.6 },
    ],
    neck: [{ d: rr(26, 51, 12, 5, 1.5), fill: 'mid', stroke: 'ink', w: 1 }],
  },
  overalls: {
    torso: 'light',
    sleeve: ['light', 7 / 19],
    pants: ['denim', 17 / 19],
    details: [
      { d: 'M20.4,74 L43.6,74 L44,78 L20,78 Z', fill: 'denim', stroke: 'line', w: 1 },
      { d: rr(25, 63, 14, 12, 1), fill: 'denim', stroke: 'line', w: 1 },
      { d: 'M26,63 L24.3,55.5 M38,63 L39.7,55.5', stroke: 'denim', w: 2.2 },
      { d: dots([[26, 64], [38, 64]], 0.9), fill: 'accent' },
      { d: rr(29, 66, 6, 5, 0.5), stroke: 'line', w: 0.8 },
    ],
  },
  tee: { torso: 'white', sleeve: ['white', 7 / 19], pants: ['mid', 7 / 19] },
  dress: {
    torso: 'mid',
    sleeve: ['mid', 5 / 19],
    skirt: { c: 'mid', from: 66, hem: 88, hw: 16 },
    details: [{ d: rr(21.8, 64.8, 20.4, 2.6), fill: 'dark' }],
  },
  skirt: {
    torso: 'light',
    sleeve: ['light', 6 / 19],
    skirt: { c: 'dark', from: 72, hem: 86, hw: 14 },
  },
  coat: {
    torso: 'mid',
    sleeve: ['mid', 16 / 19],
    pants: ['dark', 17 / 19],
    skirt: { c: 'mid', from: 55, hem: 90, hw: 13.5 },
    details: [
      { d: rr(20.2, 72, 23.6, 2.5), fill: 'dark' },
      { d: rr(30.5, 71.6, 3, 3.3, 0.5), fill: 'accent' },
      { d: 'M32,57 V71.6 M32,74.9 V78', stroke: 'ink', w: 0.8 },
      { d: dots([[29.5, 62], [34.5, 62], [29.5, 67], [34.5, 67]], 0.8), fill: 'dark' },
      { d: 'M24,55 L29,61 L32,57 L35,61 L40,55', stroke: 'ink', w: 1 },
    ],
  },
  labcoat: {
    torso: 'dark',
    sleeve: ['white', 16 / 19],
    pants: ['dark', 17 / 19],
    skirt: { c: 'white', from: 55, hem: 90, hw: 13.5, open: true },
    details: [
      { d: rr(36, 72, 6, 5), stroke: 'ink', w: 0.8 },
      { d: 'M37.5,70.5 V74', stroke: 'accent', w: 1 },
    ],
  },
  vestbow: {
    torso: 'light',
    sleeve: ['light', 16 / 19],
    pants: ['dark', 17 / 19],
    details: [
      { d: 'M23,55 L29,55 L32,62 L35,55 L41,55 L44,78 L20,78 Z', fill: 'dark', stroke: 'line', w: 1.2 },
      { d: dots([[32, 66], [32, 70.5], [32, 75]], 0.8), fill: 'accent' },
    ],
    neck: [
      { d: 'M32,56.5 L28.3,54.3 L28.3,58.7 Z M32,56.5 L35.7,54.3 L35.7,58.7 Z', fill: 'accent', stroke: 'ink', w: 0.6 },
      { d: circ(1.1, 32, 56.5), fill: 'accent', stroke: 'ink', w: 0.6 },
    ],
  },
  spacesuit: {
    torso: 'light',
    sleeve: ['light', 16 / 19],
    hands: 'dark',
    pants: ['light', 1],
    boots: 'dark',
    details: [
      { d: 'M32,56 V78', stroke: 'ink', w: 0.8 },
      { d: rr(20.2, 73, 23.6, 2.5), fill: 'dark' },
      { d: rr(25.5, 60, 5.5, 4, 0.5), fill: 'dark' },
      { d: dots([[27, 62], [29.4, 62]], 0.6), fill: 'accent' },
    ],
    neck: [{ d: rr(24, 50.5, 16, 5, 2.5), fill: 'mid', stroke: 'ink', w: 1 }],
  },
};

function paintOf(c: BodyColors, p: Paint | undefined): string {
  if (!p || p === 'none') return 'none';
  if (p === 'shell') return c.shell;
  if (p === 'line') return c.line;
  if (p === 'eye') return c.eye;
  if (p === 'accent') return c.accent;
  return c.cloth[p];
}

// Обводка ткани: на светлой — тёмная, на тёмной — как у корпуса
const clothLine = (k: ClothKey): Paint => (k === 'light' || k === 'white' ? 'ink' : 'line');

interface Shape {
  d: string;
  fill: Paint;
  stroke: Paint;
  w: number;
  t?: string;
  plate?: boolean; // у подложки скина есть копия (корпус и ткань; детали — нет)
}

function Shapes({ list, c }: { list: Shape[]; c: BodyColors }) {
  const shift = c.shift ?? 1.8;
  return (
    <>
      {c.back && (
        <g transform={`translate(${shift} ${shift})`}>
          {list
            .filter((s) => s.plate)
            .map((s, i) => (
              <path key={i} d={s.d} fill={c.back} transform={s.t} />
            ))}
        </g>
      )}
      {list.map((s, i) => (
        <path
          key={i}
          d={s.d}
          fill={paintOf(c, s.fill)}
          stroke={paintOf(c, s.stroke)}
          strokeWidth={s.stroke !== 'none' ? s.w : undefined}
          strokeLinecap="round"
          strokeLinejoin="round"
          transform={s.t}
        />
      ))}
    </>
  );
}

function geomOf(kind: BodyKind): Geom {
  return kind === 'chibi' ? CHIBI : BASE;
}

// Половина ширины туловища (обычного) на высоте y — подол продолжает его бока
const torsoL = (y: number) => 23 - (3 * (y - 55)) / 23;
const torsoR = (y: number) => 41 + (3 * (y - 55)) / 23;

/** Верх: туловище, подол, детали, руки (с рукавами), шарниры, вещи на шее —
 *  в координатах стоя (опускание сидя — у вызывающего). */
export function AvatarBodyUpper({ kind, pose = STAND_POSE, c }: { kind: BodyKind; pose?: BodyPose; c: BodyColors }) {
  const g = geomOf(kind);
  const o = OUTFITS[kind] ?? {};
  const list: Shape[] = [];
  list.push({ d: g.torso, fill: o.torso ?? 'shell', stroke: 'line', w: 1.5, plate: true });
  if (o.skirt) {
    const { from, hw, open } = o.skirt;
    // Сидя — подол до колен (иначе закрыл бы ноги до пола)
    const hemMax = pose.seat === 'floor' ? 94 - pose.dy : pose.seat ? 91 - pose.dy : Infinity;
    const hem = Math.min(o.skirt.hem, hemMax);
    const sk = { fill: o.skirt.c as Paint, stroke: clothLine(o.skirt.c), w: 1.2, plate: true };
    if (open) {
      list.push({ d: `M${torsoL(from)},${from} L30,${from} L30,${hem} L${32 - hw},${hem} Z`, ...sk });
      list.push({ d: `M34,${from} L${torsoR(from)},${from} L${32 + hw},${hem} L34,${hem} Z`, ...sk });
    } else {
      list.push({ d: `M${torsoL(from)},${from} L${torsoR(from)},${from} L${32 + hw},${hem} L${32 - hw},${hem} Z`, ...sk });
    }
  }
  for (const p of o.details ?? []) list.push({ d: p.d, fill: p.fill ?? 'none', stroke: p.stroke ?? 'none', w: p.w ?? 1 });
  const len = g.armLen * pose.armFrac;
  [0, 1].forEach((i) => {
    const t = `rotate(${pose.angles[i]} ${g.pivot[i]} ${g.armY})`;
    list.push({ d: rr(g.armX[i], g.armY, g.armW, len, g.armRx), fill: o.hands ?? 'shell', stroke: 'line', w: 1.2, t, plate: true });
    if (o.sleeve) {
      const [k, frac] = o.sleeve;
      list.push({ d: rr(g.armX[i] - 0.4, g.armY - 0.4, g.armW + 0.8, len * frac, 1), fill: k, stroke: clothLine(k), w: 1.2, t, plate: true });
    }
  });
  if (o.joints) {
    [0, 1].forEach((i) => list.push({ d: circ(2.4, g.pivot[i], g.armY + 1), fill: 'shell', stroke: 'line', w: 1.2, plate: true }));
  }
  for (const p of o.neck ?? []) list.push({ d: p.d, fill: p.fill ?? 'none', stroke: p.stroke ?? 'none', w: p.w ?? 1 });
  return <Shapes list={list} c={c} />;
}

/** Ноги по позе: стоя (со штанинами, ботинками, ступнями чиби, коленями
 *  шарниров), на стуле (бёдра к зрителю и голени), на полу по-турецки. */
export function AvatarBodyLegs({ kind, pose = STAND_POSE, c }: { kind: BodyKind; pose?: BodyPose; c: BodyColors }) {
  const g = geomOf(kind);
  const o = OUTFITS[kind] ?? {};
  const list: Shape[] = [];
  const pantsK = o.pants?.[0];
  if (pose.seat === 'floor') {
    list.push({ d: rr(12, 88, 40, 9, 3), fill: pantsK ?? 'shell', stroke: 'line', w: 1.2, plate: true });
    // Шов между ногами: у скина без обводки — цветом подложки
    return (
      <>
        <Shapes list={list} c={c} />
        <line x1="32" y1="89" x2="32" y2="96" stroke={c.line === 'none' ? (c.back ?? c.shell) : c.line} strokeWidth="1" />
      </>
    );
  }
  if (pose.seat) {
    const shin: Paint = o.boots ?? (o.pants && o.pants[1] > 0.6 ? o.pants[0] : 'shell');
    list.push({ d: rr(23, 89, 6, 8), fill: shin, stroke: 'line', w: 1.2, plate: true });
    list.push({ d: rr(35, 89, 6, 8), fill: shin, stroke: 'line', w: 1.2, plate: true });
    list.push({ d: rr(19, 84, 26, 6), fill: pantsK ?? 'shell', stroke: 'line', w: 1.2, plate: true });
    return <Shapes list={list} c={c} />;
  }
  for (const [x, y, w, h] of g.legs) {
    list.push({ d: rr(x, y, w, h), fill: 'shell', stroke: 'line', w: 1.2, plate: true });
    if (o.pants) {
      const [k, frac] = o.pants;
      list.push({ d: rr(x - 0.4, y - 0.4, w + 0.8, h * frac), fill: k, stroke: clothLine(k), w: 1.2, plate: true });
    }
    if (o.boots) list.push({ d: rr(x - 1, 91.5, w + 2, 5.5, 1.5), fill: o.boots, stroke: 'line', w: 1.2, plate: true });
    if (o.joints) list.push({ d: circ(2, x + w / 2, y + h * 0.53), fill: 'shell', stroke: 'line', w: 1.2, plate: true });
  }
  for (const [x, y, w, h] of g.feet ?? []) list.push({ d: rr(x, y, w, h, h / 2), fill: 'shell', stroke: 'line', w: 1.2, plate: true });
  return <Shapes list={list} c={c} />;
}

/** Тело для кнопок конструктора: стоя, серой тканью, без головы. */
export function BodyIcon({ kind, c, height = 26 }: { kind: BodyKind; c: BodyColors; height?: number }) {
  return (
    <svg width={(height * 40) / 48} height={height} viewBox="12 52 40 48" aria-hidden="true" style={{ display: 'block' }}>
      <AvatarBodyLegs kind={kind} c={c} />
      <AvatarBodyUpper kind={kind} c={c} />
    </svg>
  );
}

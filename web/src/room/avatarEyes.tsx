/* Глаза аватара: 22 вида — индекс RoomAvatarPreset.eyes (первые пять —
   исходные: кольцо, квадрат, черта, дуга, звезда). Рисуются фигурами вокруг
   центра глаза — одинаково у живой головы (portrait/LiveHead), фигуры в
   комнате (AvatarFigure) и картинок (иконки, листы виджета); глиф шрифта на
   22–36 px расплывается и у разных шрифтов разный.

   Другие виды — ещё и эмоции на пару секунд (EXPR): гладят — сердце, думает —
   точечки, пришло новое — удивление, нет связи с ядром — сонные; в покое
   изредка мелькает спокойная (motion.useIdleExpression). */

// Крупнее исходных (×1.3) и чуть шире расставлены
export const EYE_SCALE = 1.3;
export const EYE_X = [25, 39] as const;
export const EYE_Y = 36;

/** Номера видов-эмоций (индексы в EYE_DEFS) */
export const EXPR = {
  happy: 10, // ^ ^
  squint: 11, // > <
  sleepy: 12,
  dot: 5,
  oval: 13,
  cross: 8,
  heart: 16,
  dots: 17,
  angry: 18,
  sad: 19,
  surprise: 20,
} as const;

type Paint = 'eye' | 'shell';
interface Part {
  d: string;
  fill?: Paint;
  stroke?: Paint;
  width?: number;
}
interface EyeDef {
  parts: Part[];
  /** Правый глаз — зеркально (> <, сердитые) */
  mirror?: boolean;
}

const circ = (r: number, cx = 0, cy = 0) =>
  `M${cx - r},${cy} A${r},${r} 0 1 0 ${cx + r},${cy} A${r},${r} 0 1 0 ${cx - r},${cy} Z`;

export const EYE_DEFS: EyeDef[] = [
  { parts: [{ d: circ(3.2), stroke: 'eye', width: 1.3 }, { d: circ(1.3), fill: 'eye' }] }, // кольцо
  { parts: [{ d: 'M-2.6,-2.6 H2.6 V2.6 H-2.6 Z', fill: 'eye' }] }, // квадрат
  { parts: [{ d: 'M-3.8,0 H3.8', stroke: 'eye', width: 2 }] }, // черта
  { parts: [{ d: 'M-3.6,1.6 Q0,-3.6 3.6,1.6', stroke: 'eye', width: 1.8 }] }, // дуга
  { parts: [{ d: 'M0,-4 L1.2,-1.2 L4,0 L1.2,1.2 L0,4 L-1.2,1.2 L-4,0 L-1.2,-1.2 Z', fill: 'eye' }] }, // звезда
  { parts: [{ d: circ(2.6), fill: 'eye' }] }, // точка
  { parts: [{ d: circ(3.4), fill: 'eye' }, { d: circ(1.05, 1.2, -1.2), fill: 'shell' }] }, // блик
  { parts: [{ d: 'M-1.3,-3.8 H1.3 V3.8 H-1.3 Z', fill: 'eye' }] }, // штрих
  { parts: [{ d: 'M-3,-3 L3,3 M3,-3 L-3,3', stroke: 'eye', width: 1.8 }] }, // крест
  { parts: [{ d: 'M-3.5,0 H3.5 M0,-3.5 V3.5', stroke: 'eye', width: 1.8 }] }, // плюс
  { parts: [{ d: 'M-3.6,1.8 L0,-2 L3.6,1.8', stroke: 'eye', width: 1.8 }] }, // галка
  { parts: [{ d: 'M-2.4,-3 L2.4,0 L-2.4,3', stroke: 'eye', width: 1.8 }], mirror: true }, // прищур
  { parts: [{ d: 'M-3.4,0 A3.4,3.4 0 0 0 3.4,0 Z', fill: 'eye' }, { d: 'M-4,0 H4', stroke: 'eye', width: 1.4 }] }, // сонные
  { parts: [{ d: 'M0,-3.8 A2.2,3.8 0 1 0 0,3.8 A2.2,3.8 0 1 0 0,-3.8 Z', fill: 'eye' }] }, // овал
  { parts: [{ d: 'M-3.6,-1.5 H3.6 M-3.6,1.5 H3.6', stroke: 'eye', width: 1.4 }] }, // равно
  { parts: [{ d: 'M0,-3.4 L3.4,0 L0,3.4 L-3.4,0 Z', fill: 'eye' }] }, // ромбик
  { parts: [{ d: 'M0,3.4 C-4.6,0.4 -3.6,-3.8 0,-1.3 C3.6,-3.8 4.6,0.4 0,3.4 Z', fill: 'eye' }] }, // сердце
  { parts: [{ d: circ(1.5), fill: 'eye' }] }, // точечки
  { parts: [{ d: 'M-3.6,-1.8 L3.6,1.4', stroke: 'eye', width: 2 }], mirror: true }, // сердитые
  { parts: [{ d: 'M-3.6,-1.2 Q0,3.6 3.6,-1.2', stroke: 'eye', width: 1.8 }] }, // грусть
  { parts: [{ d: circ(3), stroke: 'eye', width: 1.5 }] }, // пустое кольцо
  { parts: [{ d: 'M-3,-3 H3 V3 H-3 Z', stroke: 'eye', width: 1.2 }, { d: circ(1.2), fill: 'eye' }] }, // экран
];

export const EYE_COUNT = EYE_DEFS.length;

/** Мимолётные эмоции в покое: в комнате — по настроению персоны (valence
 *  −1…1, arousal 0…1 из живого состояния), где его нет — спокойные. */
export function idlePool(mood?: { valence: number; arousal: number } | null): number[] {
  if (mood && mood.valence > 0.35) return [EXPR.happy, EXPR.squint];
  if (mood && mood.valence < -0.35) return mood.arousal > 0.6 ? [EXPR.angry] : [EXPR.sad];
  return [EXPR.happy, EXPR.dot, EXPR.oval];
}

// Закрытые (моргнула, спит, гладят по старой рисовке скина)
const CLOSED: EyeDef = { parts: [{ d: 'M-3.5,-0.5 Q0,3 3.5,-0.5', stroke: 'eye', width: 2 }] };

function eyeDef(kind: number): EyeDef {
  return EYE_DEFS[kind] ?? EYE_DEFS[0];
}

function OneEye({ def, x, right, color, shell }: { def: EyeDef; x: number; right: boolean; color: string; shell: string }) {
  const paint = (p?: Paint) => (p === 'eye' ? color : p === 'shell' ? shell : 'none');
  const mirror = right && def.mirror ? ' scale(-1 1)' : '';
  return (
    <g transform={`translate(${x} ${EYE_Y}) scale(${EYE_SCALE})${mirror}`}>
      {def.parts.map((p, i) => (
        <path
          key={i}
          d={p.d}
          fill={paint(p.fill)}
          stroke={paint(p.stroke)}
          strokeWidth={p.stroke ? p.width : undefined}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ))}
    </g>
  );
}

/** Пара глаз в координатах головы (центр головы — 32, 34). shell — цвет
 *  корпуса: у «блика» им вырезан отсвет. */
export function AvatarEyes({ kind, color, shell, closed = false }: { kind: number; color: string; shell: string; closed?: boolean }) {
  const def = closed ? CLOSED : eyeDef(kind);
  return (
    <>
      {EYE_X.map((x, i) => (
        <OneEye key={x} def={def} x={x} right={i === 1} color={color} shell={shell} />
      ))}
    </>
  );
}

/** Глаза для кнопок конструктора: цвет — от текста кнопки. */
export function EyesIcon({ kind, height = 12 }: { kind: number; height?: number }) {
  return (
    <svg width={height * 2.4} height={height} viewBox="16 30 32 13.3" aria-hidden="true" style={{ display: 'block' }}>
      <AvatarEyes kind={kind} color="currentColor" shell="var(--bg-panel)" />
    </svg>
  );
}

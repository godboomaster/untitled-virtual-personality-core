import type { ReactNode } from 'react';
import type { SkinAvatarProps, SkinFigurePaint } from '../types';
import { HEAD_GEOM, accessoryDraw, accessoryShift, headLift } from '../../src/room/avatarHead';
import type { AccessoryColors } from '../../src/room/avatarHead';
import { accessoryOptions } from '../../src/room/avatarOptions';
import { AvatarEyes, EYE_SCALE, EYE_X, EYE_Y } from '../../src/room/avatarEyes';

/* Голова персоны в «Чистом нейтральном» — «Вырезка»: фигурка из цветной
   бумаги. Острые края, подложка со сдвигом без размытия, каждая фигурка чуть
   повёрнута, будто наклеена от руки. Облик — из конструктора: форма та же,
   оттенок — цвет бумаги (олива, шалфей, крафт, белая), глаза и рот вырезаны
   тушью; антенна — флажок на булавке, уши — загнутые уголки, ореол — бумажная
   дуга. Движение (взгляд слоями, моргание, рот) — общее, из LiveHead.
   Фигура в комнате — та же бумага: голова отсюда (headOnly), тело красит
   figurePaint, позы рисует сама комната (room/AvatarFigure). */

type Scheme = SkinAvatarProps['scheme'];

const PALETTE: Record<Scheme, { tones: string[]; eyes: string[]; back: string; flag: string; pin: string }> = {
  light: {
    tones: ['#4F5844', '#9BB07F', '#E2C8A6', '#FBF8F1'],
    eyes: ['#FBF8F1', '#2D2E29', '#2D2E29', '#2D2E29'],
    back: '#CDC2B0',
    flag: '#5E7449',
    pin: '#55564D',
  },
  dark: {
    tones: ['#4A5440', '#7E9465', '#B89F80', '#E5DCCB'],
    eyes: ['#ECE7DA', '#1B1C18', '#1B1C18', '#1B1C18'],
    back: '#0E0F0C',
    flag: '#A9C08A',
    pin: '#C4BFB1',
  },
};

// Номер формы — для наклона «от руки»; у исходных четырёх номера прежние,
// чтобы уже наклеенные фигурки не повернулись иначе
const SHAPE_INDEX: Record<SkinAvatarProps['preset']['head'], number> = {
  circle: 0, square: 1, hex: 2, diamond: 3,
  squircle: 4, hexv: 5, octagon: 6, pent: 7, capsule: 8, screen: 9, can: 10,
};
// Подложка: тот же рисунок со сдвигом вправо-вниз
const BACK_SHIFT = 'translate(1.8 1.8)';

// Контур — общий с обычной головой (room/avatarHead), цвет и подложка — свои
function HeadShape({ head, fill }: { head: SkinAvatarProps['preset']['head']; fill?: string }) {
  return <path d={(HEAD_GEOM[head] ?? HEAD_GEOM.circle).d} fill={fill} />;
}

// Свои «вырезанные» глаза — у исходных пяти видов; размер и места — общие
// с обычной головой (room/avatarEyes). Остальные виды и эмоции — общие фигуры
function Eye({ kind, x, color }: { kind: number; x: number; color: string }) {
  return <g transform={`translate(${x} ${EYE_Y}) scale(${EYE_SCALE}) translate(${-x} ${-EYE_Y})`}>{eyeAt(kind, x, color)}</g>;
}

function eyeAt(kind: number, x: number, color: string) {
  const y = EYE_Y;
  switch (kind) {
    case 1:
      return <rect x={x - 2.7} y={y - 2.7} width="5.4" height="5.4" fill={color} />;
    case 2:
      return <rect x={x - 3.8} y={y - 1.1} width="7.6" height="2.2" fill={color} />;
    case 3:
      return (
        <path
          d={`M${x - 3.8} ${y + 1.8} Q${x} ${y - 4.2} ${x + 3.8} ${y + 1.8} L${x + 2.4} ${y + 1.8} Q${x} ${y - 1.8} ${x - 2.4} ${y + 1.8}Z`}
          fill={color}
        />
      );
    case 4:
      return (
        <path
          d={`M${x} ${y - 4}L${x + 1.2} ${y - 1.2}L${x + 4} ${y}L${x + 1.2} ${y + 1.2}L${x} ${y + 4}L${x - 1.2} ${y + 1.2}L${x - 4} ${y}L${x - 1.2} ${y - 1.2}Z`}
          fill={color}
        />
      );
    default:
      return <circle cx={x} cy={y} r="3" fill={color} />;
  }
}

// Бумажная деталь с подложкой
function Paper({ back, fill, children }: { back: string; fill: string; children: ReactNode }) {
  return (
    <>
      <g transform={BACK_SHIFT} fill={back}>
        {children}
      </g>
      <g fill={fill}>{children}</g>
    </>
  );
}

// Тело фигуры в комнате: бумага цвета оттенка и та же подложка
export const figurePaint: SkinFigurePaint = (preset, scheme) => {
  const p = PALETTE[scheme] ?? PALETTE.light;
  const shade = Math.min(3, Math.max(0, preset.shade | 0));
  // Одежда — та же цветная бумага: тёмная олива, крафт, белая; «джинса» — флажок
  const cloth = {
    dark: p.tones[0],
    mid: p.tones[2],
    light: p.tones[3],
    white: scheme === 'dark' ? '#F2EEE4' : '#FFFFFF',
    denim: p.flag,
    ink: p.pin,
  };
  return { fill: p.tones[shade], back: p.back, shift: 1.8, cloth };
};

export default function CutoutHead({ preset, eyes, scheme, closed, mouth, layers, headOnly, smile }: SkinAvatarProps) {
  const p = PALETTE[scheme] ?? PALETTE.light;
  const shade = Math.min(3, Math.max(0, preset.shade | 0));
  const tone = p.tones[shade];
  const ink = p.eyes[shade];
  // Наклон «от руки»: свой у каждого облика, постоянный
  const tilt = (((SHAPE_INDEX[preset.head] ?? 0) * 5 + shade * 3 + preset.eyes * 2 + preset.accessory) % 7 - 3) * 1.1;
  // Голова над туловищем с зазором (как у обычной). headOnly — голову ставит
  // фигура в комнате (room/AvatarFigure), здесь не поднимаем второй раз
  const lift = headOnly ? 0 : headLift(preset.head);
  const accShift = accessoryShift(preset.head);
  // Вид глаз сейчас (эмоция) — отдельно от облика: наклон считается по preset
  const eyeKind = eyes ?? preset.eyes;
  // Новые аксессуары (с пятого) — общей рисовкой в бумажных цветах: подложка
  // одним цветом со сдвигом, сверху — бумага, проволока — булавкой, акцент — флажком
  const extraKind = preset.accessory >= 4 ? accessoryOptions[preset.accessory] : undefined;
  const backTone: AccessoryColors = { shell: p.back, stroke: p.back, accent: p.back, dark: p.back, cloth: p.back };
  const paperTone: AccessoryColors = { shell: tone, stroke: p.pin, accent: p.flag, dark: p.back, cloth: p.flag };
  const extraBack = extraKind ? accessoryDraw(extraKind, backTone) : null;
  const extra = extraKind ? accessoryDraw(extraKind, paperTone) : null;

  return (
    <>
      {!headOnly && (
        <Paper back={p.back} fill={tone}>
          <path d="M23 55 L41 55 L44 78 L20 78 Z" />
          <rect x="15" y="57" width="5" height="19" transform="rotate(6 17.5 57)" />
          <rect x="44" y="57" width="5" height="19" transform="rotate(-6 46.5 57)" />
        </Paper>
      )}
      <g transform={lift ? `translate(0 ${lift})` : undefined}>
        <g {...layers.head}>
          <g transform={`rotate(${tilt.toFixed(2)} 32 34)`}>
            <g transform={accShift ? `translate(0 ${accShift})` : undefined}>
              {preset.accessory === 0 && (
                <g {...layers.accessory}>
                  <path d="M32 4 L40 6.4 L32 8.8 Z" transform={BACK_SHIFT} fill={p.back} />
                  <line x1="32" y1="15" x2="32" y2="3.4" stroke={p.pin} strokeWidth="1.2" />
                  <path d="M32 4 L40 6.4 L32 8.8 Z" fill={p.flag} />
                </g>
              )}
              {preset.accessory === 1 && (
                <Paper back={p.back} fill={tone}>
                  <path d="M13.5 18 L19 8.5 L24 16 Z" />
                  <path d="M50.5 18 L45 8.5 L40 16 Z" />
                </Paper>
              )}
              {preset.accessory === 2 && (
                <g {...layers.accessory}>
                  <Paper back={p.back} fill={p.flag}>
                    <path d="M20.5 9.6 Q32 2.4 43.5 9.6 L42.6 11.2 Q32 4.8 21.4 11.2 Z" />
                  </Paper>
                </g>
              )}
              {extra?.back && (
                <g {...(extra.sway ? layers.accessory : {})}>
                  <g transform={BACK_SHIFT}>{extraBack?.back}</g>
                  {extra.back}
                </g>
              )}
            </g>
            <g transform={BACK_SHIFT}>
              <HeadShape head={preset.head} fill={p.back} />
            </g>
            <HeadShape head={preset.head} fill={tone} />
            {extra?.front && (
              <g transform={accShift ? `translate(0 ${accShift})` : undefined}>
                <g transform={BACK_SHIFT}>{extraBack?.front}</g>
                {extra.front}
              </g>
            )}
            <g {...layers.face}>
              {closed ? (
                <AvatarEyes kind={0} closed color={ink} shell={tone} />
              ) : (
                <g {...layers.eyes}>
                  {eyeKind < 5 ? (
                    EYE_X.map((x) => <Eye key={x} kind={eyeKind} x={x} color={ink} />)
                  ) : (
                    <AvatarEyes kind={eyeKind} color={ink} shell={tone} />
                  )}
                </g>
              )}
              {smile && mouth === 0 && (
                <path d="M28.6 44.4 Q32 47.6 35.4 44.4 L34 44.4 Q32 46.1 30 44.4 Z" fill={ink} />
              )}
              {mouth === 1 && <rect x="29.4" y="43.9" width="5.2" height="2" fill={ink} />}
              {mouth === 2 && <ellipse cx="32" cy="45" rx="2.6" ry="2.3" fill={ink} />}
            </g>
          </g>
        </g>
      </g>
    </>
  );
}

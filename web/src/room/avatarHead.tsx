import type { ReactNode, SVGProps } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import { accessoryOptions, avatarTone } from './avatarOptions';

/* Голова аватара из «Комнаты»: форма и аксессуар. Общая для фигуры в сцене
   (AvatarFigure) и аватара персоны в чатах и списках (PersonaAvatar), чтобы
   облик был один. Координаты — бокс фигуры 64×100, центр головы (32, 34),
   верх туловища — y=55. */

type HeadKind = RoomAvatarPreset['head'];

/** Контур каждой формы головы и её верх/низ: по низу голова встаёт над
 *  туловищем с одним зазором, к верху крепятся антенна, уши и ореол.
 *  Скин со своей рисовкой берёт контуры отсюда же (web/skins/<id>/avatar.tsx). */
export const HEAD_GEOM: Record<HeadKind, { d: string; top: number; bottom: number }> = {
  circle: { d: 'M12,34 A20,20 0 1 0 52,34 A20,20 0 1 0 12,34 Z', top: 14, bottom: 54 },
  square: { d: 'M12,14 H52 V54 H12 Z', top: 14, bottom: 54 },
  hex: { d: 'M53,34 L42.5,52.2 L21.5,52.2 L11,34 L21.5,15.8 L42.5,15.8 Z', top: 15.8, bottom: 52.2 },
  diamond: { d: 'M32,12 L52,34 L32,56 L12,34 Z', top: 12, bottom: 56 },
  squircle: { d: 'M21,14 H43 Q52,14 52,23 V45 Q52,54 43,54 H21 Q12,54 12,45 V23 Q12,14 21,14 Z', top: 14, bottom: 54 },
  hexv: { d: 'M32,13 L50.19,23.5 L50.19,44.5 L32,55 L13.81,44.5 L13.81,23.5 Z', top: 13, bottom: 55 },
  octagon: {
    d: 'M23.77,14.14 L40.23,14.14 L51.86,25.77 L51.86,42.23 L40.23,53.86 L23.77,53.86 L12.14,42.23 L12.14,25.77 Z',
    top: 14.14,
    bottom: 53.86,
  },
  pent: { d: 'M32,13 L52.92,28.2 L44.93,52.8 L19.07,52.8 L11.08,28.2 Z', top: 13, bottom: 52.8 },
  capsule: { d: 'M26,18 H38 A16,16 0 0 1 38,50 H26 A16,16 0 0 1 26,18 Z', top: 18, bottom: 50 },
  screen: { d: 'M15,17 H49 Q55,17 55,23 V46 Q55,52 49,52 H15 Q9,52 9,46 V23 Q9,17 15,17 Z', top: 17, bottom: 52 },
  can: { d: 'M12,20 A20,6 0 0 1 52,20 V48 A20,6 0 0 1 12,48 Z', top: 14, bottom: 54 },
};

// Зазор между низом головы и верхом туловища (y=55) — одинаковый у всех форм
const BODY_TOP = 55;
export const NECK_GAP = 5;

// Версия рисовки: в ключах кэша картинок головы (иконки уведомлений, листы
// виджета телефона) — сменилась рисовка, картинки печатаются заново
export const HEAD_DRAW_VERSION = 'g5-eyes22-acc10-body16';

function geom(head: HeadKind) {
  return HEAD_GEOM[head] ?? HEAD_GEOM.circle;
}

/** Сдвиг головы по вертикали (вверх — отрицательный): низ формы — над
 *  туловищем на NECK_GAP. Двигается голова целиком: форма, аксессуар, лицо. */
export function headLift(head: HeadKind, bodyTop = BODY_TOP): number {
  return bodyTop - NECK_GAP - geom(head).bottom;
}

/** Сдвиг аксессуара к верху формы (нарисован для верха y=14). Выше 14 не
 *  поднимаем: у ромба и «стоящих» форм антенна и так уходит в вершину. */
export function accessoryShift(head: HeadKind): number {
  return Math.max(0, geom(head).top - 14);
}

/** Силуэт формы для кнопок конструктора (цвет — от текста кнопки). */
export function HeadShapeIcon({ head, size = 14 }: { head: HeadKind; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="7 9 50 50" aria-hidden="true" style={{ display: 'block' }}>
      <path d={geom(head).d} fill="currentColor" />
    </svg>
  );
}

export interface AccessoryColors {
  shell: string; // корпус
  stroke: string; // обводка и проволока
  accent: string; // огонёк, нимб, бант, листья
  dark: string; // шляпа
  cloth: string; // наушники
}

type AccessoryKind = (typeof accessoryOptions)[number];

/** Рисунок аксессуара в координатах головы (верх головы — y=14): back — за
 *  головой, front — поверх (уборы, наушники); sway — back отстаёт при
 *  повороте головы (висит на проволоке: антенны, нимб, росток). Общий для
 *  обычной головы и скинов (свои цвета). */
export function accessoryDraw(kind: AccessoryKind, c: AccessoryColors): { back?: ReactNode; front?: ReactNode; sway?: boolean } {
  switch (kind) {
    case 'antenna':
      return {
        sway: true,
        back: (
          <>
            <line x1="32" y1="14" x2="32" y2="6" stroke={c.stroke} strokeWidth="2" />
            <circle cx="32" cy="4.5" r="2.5" fill={c.accent} />
          </>
        ),
      };
    case 'ears':
      return {
        back: (
          <>
            <rect x="13" y="9" width="7" height="7" fill={c.shell} stroke={c.stroke} strokeWidth="1.5" />
            <rect x="44" y="9" width="7" height="7" fill={c.shell} stroke={c.stroke} strokeWidth="1.5" />
          </>
        ),
      };
    case 'halo':
      return { sway: true, back: <ellipse cx="32" cy="7" rx="11" ry="3" fill="none" stroke={c.accent} strokeWidth="1.5" /> };
    case 'twin':
      return {
        sway: true,
        back: (
          <>
            <path d="M26,15 L22,6 M38,15 L42,6" fill="none" stroke={c.stroke} strokeWidth="2" strokeLinecap="round" />
            <circle cx="21.6" cy="5" r="2" fill={c.accent} />
            <circle cx="42.4" cy="5" r="2" fill={c.accent} />
          </>
        ),
      };
    case 'spring':
      return {
        sway: true,
        back: (
          <>
            <path
              d="M32,14 L29.5,12.5 L34.5,10.5 L29.5,8.5 L34.5,6.5 L32,5"
              fill="none"
              stroke={c.stroke}
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <circle cx="32" cy="3.2" r="2.2" fill={c.accent} />
          </>
        ),
      };
    case 'headphones':
      // Дуга — с обводкой (тёмная дуга на тёмной голове иначе пропадает)
      return {
        front: (
          <>
            <path d="M12.5,31 A19.5,19.5 0 0 1 51.5,31" fill="none" stroke={c.stroke} strokeWidth="4.4" />
            <path d="M12.5,31 A19.5,19.5 0 0 1 51.5,31" fill="none" stroke={c.cloth} strokeWidth="3" />
            <rect x="7.5" y="27" width="6.5" height="13" rx="2.5" fill={c.cloth} stroke={c.stroke} strokeWidth="1.2" />
            <rect x="50" y="27" width="6.5" height="13" rx="2.5" fill={c.cloth} stroke={c.stroke} strokeWidth="1.2" />
          </>
        ),
      };
    case 'tophat':
      // Низкий цилиндр: тулья 9, лента, поля
      return {
        front: (
          <>
            <rect x="21" y="4" width="22" height="9" rx="1" fill={c.dark} stroke={c.stroke} strokeWidth="1.2" />
            <rect x="21" y="10" width="22" height="2.2" fill={c.accent} />
            <rect x="14.5" y="12.5" width="35" height="3.2" rx="1.6" fill={c.dark} stroke={c.stroke} strokeWidth="1.2" />
          </>
        ),
      };
    case 'bow':
      return {
        front: (
          <g fill={c.accent} stroke={c.stroke} strokeWidth="1" strokeLinejoin="round">
            <path d="M40,14 L33,8.5 L33,19.5 Z" />
            <path d="M40,14 L47,8.5 L47,19.5 Z" />
            <circle cx="40" cy="14" r="2.2" />
          </g>
        ),
      };
    case 'sprout':
      return {
        sway: true,
        back: (
          <>
            <path d="M32,14 Q31,10 32,6" fill="none" stroke={c.stroke} strokeWidth="1.6" strokeLinecap="round" />
            <path d="M32,9 Q24.5,3.5 23,9 Q27.5,11.5 32,9 Z" fill={c.accent} />
            <path d="M32,7 Q39.5,0.5 41.5,6 Q36.5,9 32,7 Z" fill={c.accent} />
          </>
        ),
      };
    default:
      return {};
  }
}

/** Аксессуар для кнопок конструктора: верх головы (своей формы и оттенка) с ним. */
export function AccessoryIcon({ config, height = 20 }: { config: RoomAvatarPreset; height?: number }) {
  return (
    <svg width={(height * 60) / 44} height={height} viewBox="2 -4 60 44" aria-hidden="true" style={{ display: 'block' }}>
      <AvatarHeadShape config={config} />
    </svg>
  );
}

// accessoryLayer — тому, что висит на проволоке (антенны, нимб, росток): у
// живой головы оно поворачивается чуть иначе, чем сама голова
// (portrait/LiveHead); accent — его цвет, где нет CSS-переменных темы
// (картинка уведомления, атлас виджета)
export function AvatarHeadShape({
  config,
  accessoryLayer,
  accent = 'var(--accent)',
}: {
  config: RoomAvatarPreset;
  accessoryLayer?: SVGProps<SVGGElement>;
  accent?: string;
}) {
  const { shade, stroke } = avatarTone(config);
  const kind = accessoryOptions[config.accessory] ?? 'none';
  const shift = accessoryShift(config.head);
  const shiftT = shift ? `translate(0 ${shift})` : undefined;
  const art = accessoryDraw(kind, { shell: shade, stroke, accent, dark: '#151515', cloth: '#2b2b2b' });
  return (
    <>
      {art.back && <g transform={shiftT}>{art.sway ? <g {...accessoryLayer}>{art.back}</g> : art.back}</g>}
      <path d={geom(config.head).d} fill={shade} stroke={stroke} strokeWidth="1.5" />
      {art.front && <g transform={shiftT}>{art.front}</g>}
    </>
  );
}

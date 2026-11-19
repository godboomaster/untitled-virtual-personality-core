import { useContext, useRef } from 'react';
import type { Ref, SVGProps } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import { AvatarHeadShape, headLift } from '../room/avatarHead';
import { AvatarBodyUpper, CLOTH_GRAY, bodyKindOf } from '../room/avatarBody';
import { avatarTone } from '../room/avatarOptions';
import { AvatarEyes, EXPR, idlePool } from '../room/avatarEyes';
import { HeadLookContext } from './headLook';
import type { HeadLook } from './headLook';
import { useBlink, useGaze, useIdleExpression, useMouth } from './motion';
import type { GazeCues, Mouth } from './motion';

/* Живая голова фигуры из «Комнаты» — для персоны без своей картинки и без
   листов портрета: аватар в чатах (PersonaAvatar), голосовой режим, портрет
   на рабочем столе. Движение — тот же движок, что у живого портрета
   (motion.ts): смотрит на курсор, оглядывается, моргает, говорит, думает,
   читает, кивает; спит — глаза закрыты, голова опущена. Эмоции глазами
   (room/avatarEyes): гладят — сердце, думает — точечки, в покое изредка
   мелькает спокойная; снаружи можно подать свою (expression).

   Поворот — сдвиг слоями: голова немного, лицо на ней больше, антенна и
   нимб чуть отстают (LAYERS). Взгляд уходит в CSS-переменные --gx/--gy
   прямо на <svg>, поэтому кадр движения React не перерисовывает. Глаза —
   фигурами, а не глифами шрифта, как у фигуры в сцене: на 22–36 px глиф
   расплывается. Бокс 64×64 — голова фигуры по плечи. */

// Сдвиг слоя при взгляде в край (−1 или 1), в единицах бокса: [x, y]
const LAYERS = {
  head: [2.2, 1.8],
  face: [3, 2.6],
  accessory: [1.2, 0.5],
} as const;

type Gaze = { x: number; y: number };

// Мимолётные эмоции живой головы: настроения здесь нет — спокойные
const IDLE_CALM = idlePool(null);

// Живой (gaze нет) — сдвиг от CSS-переменных; картинка — сразу числами
function layer([kx, ky]: readonly [number, number], gaze?: Gaze): SVGProps<SVGGElement> {
  if (gaze) return { transform: `translate(${(gaze.x * kx).toFixed(2)} ${(gaze.y * ky).toFixed(2)})` };
  return { style: { transform: `translate(calc(var(--gx, 0) * ${kx}px), calc(var(--gy, 0) * ${ky}px))` } };
}

export function LiveHead({
  preset,
  label,
  accent,
  followWithin,
  onGaze,
  smile,
  look,
  expression = null,
  ...cues
}: GazeCues & {
  preset: RoomAvatarPreset;
  label: string;
  /** Цвет антенны и нимба; без него — акцент темы */
  accent?: string;
  /** Смотреть на курсор, только пока он над ближайшим предком по селектору
   *  (список, шапка); в списке голов много — оглядываются реже */
  followWithin?: string;
  /** Взгляд на каждом кадре движения (портрет на рабочем столе — для маски) */
  onGaze?: (x: number, y: number) => void;
  /** Лёгкая улыбка, пока молчит */
  smile?: boolean;
  /** Рисовка скина явно (портрет на рабочем столе: контекста там нет);
   *  не задана — из HeadLookContext */
  look?: HeadLook | null;
  /** Эмоция снаружи (вид глаз, room/avatarEyes.EXPR): пришло новое и т. п.;
   *  null — своя */
  expression?: number | null;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const gazeCb = useRef(onGaze);
  gazeCb.current = onGaze;
  useGaze(
    ref,
    cues,
    (x, y) => {
      ref.current?.style.setProperty('--gx', x.toFixed(3));
      ref.current?.style.setProperty('--gy', y.toFixed(3));
      gazeCb.current?.(x, y);
    },
    { followWithin, calm: !!followWithin },
  );
  // Спит — глаза и так закрыты: таймеры моргания не нужны
  const blink = useBlink(ref, !cues.asleep);
  const mouth = useMouth(ref, !!cues.speaking);
  // В покое — изредка спокойная эмоция; занята (говорит, думает, гладят, спит) — нет
  const busy = !!(cues.asleep || cues.speaking || cues.thinking || cues.petted);
  const idle = useIdleExpression(ref, !busy, IDLE_CALM);
  const shown = cues.petted ? EXPR.heart : cues.thinking ? EXPR.dots : expression ?? idle;
  return (
    <AvatarHeadSvg
      ref={ref}
      preset={preset}
      expression={shown}
      closed={blink || !!cues.asleep}
      mouth={mouth}
      smile={smile}
      accent={accent}
      label={label}
      look={look}
    />
  );
}

/** Голова: живая (LiveHead) или картинкой в заданном ракурсе (gaze — иконка
 *  уведомления, атласы для виджета и маски). x/y/width/height — место клетки
 *  в атласе: вложенный <svg> обрезает плечи по своей клетке. Рисовка — своя
 *  у скина (look или контекст HeadLookContext), иначе голова фигуры. */
export function AvatarHeadSvg({
  preset,
  expression = null,
  closed = false,
  mouth = 0,
  gaze,
  accent,
  label,
  ref,
  look,
  smile = false,
  ...box
}: {
  preset: RoomAvatarPreset;
  /** Вид глаз вместо своего (эмоция); null — preset.eyes */
  expression?: number | null;
  closed?: boolean;
  mouth?: Mouth;
  smile?: boolean;
  gaze?: Gaze;
  accent?: string;
  label: string;
  /** Рисовка скина явно (картинки вне дерева React); undefined — из контекста */
  look?: HeadLook | null;
  ref?: Ref<SVGSVGElement>;
  x?: number;
  y?: number;
  width?: number | string;
  height?: number | string;
}) {
  const fromContext = useContext(HeadLookContext);
  const skin = look === undefined ? fromContext : look;
  const { shade, eyeColor, stroke } = avatarTone(preset);
  const eyes = expression ?? preset.eyes;
  const Draw = skin?.draw;
  // Плечи и одежда — общей рисовкой (room/avatarBody), у скина — его краской
  // и подложкой. Чиби в бюсте — как обычное: пропорции видны только в рост
  const paint = skin?.paint?.(preset, skin.scheme);
  const kind = bodyKindOf(preset);
  const bustKind = kind === 'chibi' ? 'base' : kind;
  const bodyColors = {
    shell: paint?.fill ?? shade,
    line: paint ? (paint.stroke ?? 'none') : stroke,
    eye: eyeColor,
    accent: accent ?? 'var(--accent)',
    cloth: { ...CLOTH_GRAY, ...paint?.cloth },
    back: paint?.back,
    shift: paint?.shift,
  };
  return (
    <svg
      ref={ref}
      className="pa"
      viewBox="0 0 64 64"
      width="100%"
      height="100%"
      style={{ display: 'block' }}
      role="img"
      aria-label={label || undefined}
      aria-hidden={label ? undefined : true}
      {...box}
    >
      {/* Кадр бюста: y от −4 (антенна поднятой над туловищем головы — у ромба
          выше всех) до 60 (плечи) */}
      <g transform="translate(0 4)">
        <AvatarBodyUpper kind={bustKind} c={bodyColors} />
        <g transform={`translate(0 ${headLift(preset.head)})`}>
          {Draw && skin ? (
            // Скин рисует только голову; над туловищем её ставит этот кадр
            <Draw
              preset={preset}
              eyes={eyes}
              scheme={skin.scheme}
              closed={closed}
              mouth={mouth}
              smile={smile}
              headOnly
              layers={{
                head: layer(LAYERS.head, gaze),
                face: layer(LAYERS.face, gaze),
                accessory: layer(LAYERS.accessory, gaze),
              }}
            />
          ) : (
            <g {...layer(LAYERS.head, gaze)}>
              <AvatarHeadShape config={preset} accessoryLayer={layer(LAYERS.accessory, gaze)} accent={accent} />
              <g {...layer(LAYERS.face, gaze)}>
                <AvatarEyes kind={eyes} closed={closed} color={eyeColor} shell={shade} />
                {smile && mouth === 0 && (
                  <path d="M28.5 44.6 Q32 47.6 35.5 44.6" fill="none" stroke={eyeColor} strokeWidth="1.4" strokeLinecap="round" />
                )}
                {mouth === 1 && <rect x="29.4" y="44.1" width="5.2" height="1.8" rx="0.9" fill={eyeColor} />}
                {mouth === 2 && <ellipse cx="32" cy="45" rx="2.6" ry="2.2" fill={eyeColor} />}
              </g>
            </g>
          )}
        </g>
      </g>
    </svg>
  );
}

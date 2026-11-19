import { memo, useContext, useRef } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import { useI18n } from '../i18n';
import { HeadLookContext } from '../portrait/headLook';
import { avatarTone } from './avatarOptions';
import { AvatarEyes, EXPR, idlePool } from './avatarEyes';
import { useIdleExpression } from '../portrait/motion';
import { AvatarHeadShape, headLift } from './avatarHead';
import { AvatarBodyLegs, AvatarBodyUpper, CLOTH_GRAY, bodyKindOf, bodyTopOf } from './avatarBody';
import type { BodyColors, BodyPose } from './avatarBody';
import type { AvatarSeat } from './avatarOptions';
import { hashStr } from './roomModel';
import type { RoomClientPose, RoomPose } from './roomTypes';

/* SVG-фигура аватара: голова выбранной формы + глаза + аксессуар + тело
   (вид и одежда — room/avatarBody) в позе. Моргание и дыхание — только CSS (index.css,
   .room-av-*): период и сдвиг моргания у каждого экземпляра свои (из хэша
   seed), поэтому две фигуры не моргают синхронно. Таймеров нет.

   Координаты: бокс 64×100 (ступни — y=97 по центру), сон — отдельный бокс
   104×52 «в кровати» (y=40 — верх матраса, см. SLEEP_ANCHOR). height — рост
   стоящей фигуры в px.

   pose — поза тела (серверная); attention — клиентская реплика поверх неё:
   with_you/glance не поднимают со стула, а только поворачивают лицо к
   зрителю. seat — на чём сидит: desk — на стуле за столом (стол рисует
   сцена, лист лежит на столешнице), chair — на сиденье без стола (кровать,
   фон пользователя), floor — по-турецки на полу, null — стоит.
   dozing — дремлет (нет связи с ядром): глаза закрыты, над головой «z z».

   Скин со своей рисовкой (web/skins/<id>/avatar.tsx) рисует голову
   (headOnly) и красит тело (figurePaint: заливка и подложка без размытия);
   позы, сиденья, книга и лист — общие, отсюда. */

export interface AvatarFigureProps {
  config: RoomAvatarPreset;
  pose?: RoomClientPose;
  attention?: 'with_you' | 'glance' | null;
  seat?: AvatarSeat;
  height?: number;
  seed?: string; // разводит фазы моргания (обычно id персоны)
  title?: string;
  dozing?: boolean;
  /** Настроение (живое состояние): какие эмоции мелькают в покое */
  mood?: { valence: number; arousal: number } | null;
}

// Светлая «бумага» для книги и листа — видна в обеих темах
const PAPER = '#f1efe8';
const INK = '#9a9a9a';

function AvatarFigureImpl({ config, pose: rawPose = 'stand', attention: rawAttention = null, seat: rawSeat, height = 132, seed = '', title, dozing = false, mood = null }: AvatarFigureProps) {
  const { t } = useI18n();
  // Эмоции глазами (room/avatarEyes): в покое изредка мелькают под настроение;
  // дремлет (нет связи с ядром) — сонные; спит в кровати — закрыты
  const svgRef = useRef<SVGSVGElement>(null);
  const idle = useIdleExpression(svgRef, !dozing && rawPose !== 'sleep' && rawPose !== 'away', idlePool(mood));
  // Рисовка скина (хук — до любого раннего выхода)
  const look = useContext(HeadLookContext);
  // Старый вызов: pose='with_you'/'glance' без тела — стоя
  const legacyCue = rawPose === 'with_you' || rawPose === 'glance' ? rawPose : null;
  const pose: RoomPose = legacyCue ? 'stand' : (rawPose as RoomPose);
  const attention = rawAttention ?? legacyCue;
  if (pose === 'away') return null;
  const { shade, eyeColor, stroke } = avatarTone(config);
  const k = height / 100;
  const h = hashStr(seed || 'avatar');
  const blink = {
    animationDuration: `${6 + (h % 5)}s`,
    animationDelay: `-${(h >>> 4) % 6000}ms`,
  };
  const breath = { animationDelay: `-${(h >>> 9) % 4000}ms` };
  const label = title ?? t('room.avatarAria');
  const sleeping = pose === 'sleep';

  // Сидит: sit/write — всегда, read — только за столом/на сиденье (у полки читает стоя)
  const seat: AvatarSeat =
    pose === 'sit' || pose === 'write' ? (rawSeat ?? 'chair')
    : pose === 'read' && (rawSeat === 'desk' || rawSeat === 'chair') ? rawSeat
    : null;
  const atDesk = seat === 'desk';
  // Верх тела опускается: на стуле — на 8, на полу — на 12
  const dy = seat === 'floor' ? 12 : seat ? 8 : 0;
  const facing = attention != null; // лицом к зрителю

  // Смещение глаз: читает/пишет — вниз, смотрит в окно — вправо-вверх
  const eyeShift = facing ? undefined
    : pose === 'read' ? 'translate(0 3)'
    : pose === 'write' ? 'translate(2 3)'
    : pose === 'look' ? 'translate(4 -3)'
    : undefined;
  // Наклон головы вокруг «шеи» — середины зазора над туловищем (y=55)
  const headTilt =
    attention === 'glance' ? 'rotate(5 32 52)'
    : attention === 'with_you' ? 'rotate(-3 32 52)'
    : dozing ? 'rotate(7 32 52)'
    : pose === 'look' ? 'rotate(8 32 52)'
    : pose === 'write' || pose === 'read' ? 'rotate(-4 32 52)'
    : undefined;
  // Тело и одежда; голова целиком (форма, аксессуар, лицо) — над туловищем
  // с зазором (у чиби туловище ниже — и голова ниже)
  const bodyKind = bodyKindOf(config);
  const lift = headLift(config.head, bodyTopOf(bodyKind));

  // Рисовка скина: голова от скина, тело его краской; без скина — стандартные
  const paint = look?.paint?.(config, look.scheme);
  const bodyFill = paint?.fill ?? shade;
  const bodyStroke = paint ? (paint.stroke ?? 'none') : stroke;
  // Цвета тела: корпус — оттенок или краска скина (с подложкой), ткань — серая или скина
  const bodyColors: BodyColors = {
    shell: bodyFill,
    line: bodyStroke,
    eye: eyeColor,
    accent: 'var(--accent)',
    cloth: { ...CLOTH_GRAY, ...paint?.cloth },
    back: paint?.back,
    shift: paint?.shift,
  };
  const SkinHead = look?.draw;
  const skinHead = (closed: boolean, extra: { faceTransform?: string; blinkEyes?: boolean; smile?: boolean } = {}) =>
    SkinHead && look ? (
      <SkinHead
        preset={config}
        scheme={look.scheme}
        closed={closed}
        mouth={0}
        smile={extra.smile}
        headOnly
        layers={{
          head: {},
          accessory: {},
          face: extra.faceTransform ? { transform: extra.faceTransform } : {},
          eyes: extra.blinkEyes ? { className: 'room-av-eyes', style: blink } : undefined,
        }}
      />
    ) : null;

  const headShape = <AvatarHeadShape config={config} />;

  if (sleeping) {
    // Лёжа в кровати: голова на подушке (левый край), тело под одеялом,
    // одеяло свисает на лицевую сторону матраса; y=40 — верх матраса
    return (
      <svg
        ref={svgRef}
        width={104 * k}
        height={52 * k}
        viewBox="0 0 104 52"
        role="img"
        aria-label={label}
        className="room-av room-av--sleep"
      >
        <g transform="translate(15 27) rotate(-22) scale(0.6) translate(-32 -34)">
          {SkinHead ? (
            skinHead(true)
          ) : (
            <>
              {headShape}
              <AvatarEyes kind={config.eyes} closed color={eyeColor} shell={shade} />
            </>
          )}
        </g>
        <g className="room-av-breath" style={breath}>
          <path
            d="M25,47 L25,31 Q31,26 42,27.5 Q60,30 76,29 Q88,27 95,29.5 Q101,31.5 101,37 L101,47 Z"
            fill="var(--bg-panel-2)"
            stroke={stroke}
            strokeWidth="1.3"
            strokeLinejoin="round"
          />
          {/* Отогнутый край простыни у плеч и складки */}
          <path d="M25,31 Q31,26 33,28 L33,47 L25,47 Z" fill="var(--bg-panel)" stroke={stroke} strokeWidth="1" />
          <line x1="48" y1="36" x2="90" y2="36" stroke="var(--border)" strokeWidth="1" />
          <line x1="54" y1="41" x2="94" y2="41" stroke="var(--border)" strokeWidth="1" />
        </g>
        <g fill="var(--text-muted)" fontFamily="'JetBrains Mono', monospace">
          <text x="27" y="15" className="room-av-z" fontSize="9">z</text>
          <text x="34" y="8" className="room-av-z room-av-z--2" fontSize="7">z</text>
        </g>
      </svg>
    );
  }

  // «z z» над головой, пока дремлет
  const zzz = dozing && (
    <g fill="var(--text-muted)" fontFamily="'JetBrains Mono', monospace">
      <text x="47" y="12" className="room-av-z" fontSize="9">z</text>
      <text x="54" y="5" className="room-av-z room-av-z--2" fontSize="7">z</text>
    </g>
  );

  const head = (
    <g transform={headTilt} className={attention === 'glance' ? 'room-av-head room-av-head--glance' : 'room-av-head'}>
      <g transform={lift ? `translate(0 ${lift})` : undefined}>
        {SkinHead ? (
          skinHead(dozing, { faceTransform: eyeShift, blinkEyes: !dozing, smile: attention === 'with_you' })
        ) : (
          <>
            {headShape}
            {dozing ? (
              <AvatarEyes kind={EXPR.sleepy} color={eyeColor} shell={shade} />
            ) : (
              <g transform={eyeShift}>
                <g className="room-av-eyes" style={blink}>
                  <AvatarEyes kind={idle ?? config.eyes} color={eyeColor} shell={shade} />
                </g>
              </g>
            )}
            {attention === 'with_you' && (
              <path d="M28,45 Q32,48 36,45" fill="none" stroke={eyeColor} strokeWidth="1.2" strokeLinecap="round" />
            )}
          </>
        )}
        {zzz}
      </g>
    </g>
  );

  // Руки (поворот вокруг плеча, °; «+» у левой и «−» у правой — к центру):
  // читает — обе к книге; за столом — предплечья на столешнице (короче)
  const armL = pose === 'read' ? -28 : atDesk ? -16 : seat ? -10 : 6;
  const armR =
    pose === 'read' ? 28
    : pose === 'write' ? (atDesk ? 10 : 34)
    : atDesk ? 16
    : seat ? 10
    : attention === 'with_you' ? -14
    : -6;
  const bodyPose: BodyPose = { angles: [armL, armR], armFrac: atDesk ? 13 / 19 : seat ? 15 / 19 : 1, seat, dy };

  // Раскрытая книга на уровне груди: обложка акцентом, светлые страницы
  // (у чиби грудь ниже)
  const book = pose === 'read' && (
    <g transform={bodyKind === 'chibi' ? 'translate(0 7)' : undefined}>
      <rect x="17" y="63" width="30" height="17" fill="var(--accent)" />
      <rect x="18.5" y="64" width="13" height="14.5" fill={PAPER} stroke={INK} strokeWidth="0.6" />
      <rect x="32.5" y="64" width="13" height="14.5" fill={PAPER} stroke={INK} strokeWidth="0.6" />
      <g stroke={INK} strokeWidth="0.8">
        <line x1="21" y1="68" x2="29" y2="68" />
        <line x1="21" y1="71" x2="29" y2="71" />
        <line x1="21" y1="74" x2="27" y2="74" />
        <line x1="35" y1="68" x2="43" y2="68" />
        <line x1="35" y1="71" x2="43" y2="71" />
      </g>
    </g>
  );

  const upper = (
    <g transform={dy ? `translate(0 ${dy})` : undefined}>
      <g className="room-av-breath" style={breath}>
        <AvatarBodyUpper kind={bodyKind} pose={bodyPose} c={bodyColors} />
        {head}
        {book}
      </g>
    </g>
  );

  // Ноги по сиденью (стоя, на стуле, на полу) — со штанинами и обувью одежды
  const legs = <AvatarBodyLegs kind={bodyKind} pose={bodyPose} c={bodyColors} />;

  // Лист и ручка: за столом — на столешнице (её верх — y≈76.5 при точке
  // места стола), иначе — на коленях
  const paperY = atDesk ? 76.5 : seat === 'floor' ? 90 : 86;
  const paper = pose === 'write' && (
    <g>
      <polygon
        points={`29,${paperY} 50,${paperY} 47.5,${paperY - 5.5} 31.5,${paperY - 5.5}`}
        fill={PAPER}
        stroke={INK}
        strokeWidth="0.7"
      />
      <line x1="34" y1={paperY - 3.8} x2="44" y2={paperY - 3.8} stroke={INK} strokeWidth="0.6" />
      <line x1="33.5" y1={paperY - 1.8} x2="42" y2={paperY - 1.8} stroke={INK} strokeWidth="0.6" />
      <line x1="45" y1={paperY - 2} x2="49" y2={paperY - 10} stroke="var(--accent)" strokeWidth="1.6" strokeLinecap="round" />
    </g>
  );

  return (
    <svg
      ref={svgRef}
      width={64 * k}
      height={100 * k}
      viewBox="0 0 64 100"
      role="img"
      aria-label={label}
      className={`room-av room-av--${attention ?? pose}${seat ? ` room-av--seat-${seat}` : ''}`}
    >
      {legs}
      {upper}
      {paper}
    </svg>
  );
}

// memo: фигура не перерисовывается на каждый поллинг, если поза и конфиг те же
const AvatarFigure = memo(AvatarFigureImpl);
export default AvatarFigure;

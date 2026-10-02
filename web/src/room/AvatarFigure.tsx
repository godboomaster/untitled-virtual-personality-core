import { memo } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import { useI18n } from '../i18n';
import { accessoryOptions, avatarShades, eyeOptions } from './avatarOptions';
import type { AvatarSeat } from './avatarOptions';
import { hashStr } from './roomModel';
import type { RoomClientPose, RoomPose } from './roomTypes';

/* SVG-фигура аватара: голова выбранной формы + глаза-глифы + аксессуар +
   простое тело в позе. Моргание и дыхание — только CSS (index.css,
   .room-av-*): период и сдвиг моргания у каждого экземпляра свои (из хэша
   seed), поэтому две фигуры не моргают синхронно. Таймеров нет.

   Координаты: бокс 64×100 (ступни — y=97 по центру), сон — отдельный бокс
   104×52 «в кровати» (y=40 — верх матраса, см. SLEEP_ANCHOR). height — рост
   стоящей фигуры в px.

   pose — поза тела (серверная); attention — клиентская реплика поверх неё:
   with_you/glance не поднимают со стула, а только поворачивают лицо к
   зрителю. seat — на чём сидит: desk — на стуле за столом (стол рисует
   сцена, лист лежит на столешнице), chair — на сиденье без стола (кровать,
   фон пользователя), floor — по-турецки на полу, null — стоит. */

export interface AvatarFigureProps {
  config: RoomAvatarPreset;
  pose?: RoomClientPose;
  attention?: 'with_you' | 'glance' | null;
  seat?: AvatarSeat;
  height?: number;
  seed?: string; // разводит фазы моргания (обычно id персоны)
  title?: string;
}

// Светлая «бумага» для книги и листа — видна в обеих темах
const PAPER = '#f1efe8';
const INK = '#9a9a9a';

function AvatarFigureImpl({ config, pose: rawPose = 'stand', attention: rawAttention = null, seat: rawSeat, height = 132, seed = '', title }: AvatarFigureProps) {
  const { t } = useI18n();
  // Старый вызов: pose='with_you'/'glance' без тела — стоя
  const legacyCue = rawPose === 'with_you' || rawPose === 'glance' ? rawPose : null;
  const pose: RoomPose = legacyCue ? 'stand' : (rawPose as RoomPose);
  const attention = rawAttention ?? legacyCue;
  if (pose === 'away') return null;
  const shade = avatarShades[config.shade] ?? avatarShades[1];
  // На светлых оттенках глаза тёмные, на тёмных — светлые
  const eyeColor = config.shade >= 2 ? '#1c1c1c' : '#eae8e1';
  const stroke = config.shade >= 2 ? '#5a5a5a' : '#8f8f8f';
  const accessory = accessoryOptions[config.accessory] ?? 'none';
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
  // Наклон головы вокруг шеи
  const headTilt =
    attention === 'glance' ? 'rotate(5 32 54)'
    : attention === 'with_you' ? 'rotate(-3 32 54)'
    : pose === 'look' ? 'rotate(8 32 54)'
    : pose === 'write' || pose === 'read' ? 'rotate(-4 32 54)'
    : undefined;

  const headShape = (
    <>
      {accessory === 'antenna' && (
        <>
          <line x1="32" y1="14" x2="32" y2="6" stroke={stroke} strokeWidth="2" />
          <circle cx="32" cy="4.5" r="2.5" fill="var(--accent)" />
        </>
      )}
      {accessory === 'ears' && (
        <>
          <rect x="13" y="9" width="7" height="7" fill={shade} stroke={stroke} strokeWidth="1.5" />
          <rect x="44" y="9" width="7" height="7" fill={shade} stroke={stroke} strokeWidth="1.5" />
        </>
      )}
      {accessory === 'halo' && (
        <ellipse cx="32" cy="7" rx="11" ry="3" fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      )}
      {config.head === 'circle' && <circle cx="32" cy="34" r="20" fill={shade} stroke={stroke} strokeWidth="1.5" />}
      {config.head === 'square' && <rect x="12" y="14" width="40" height="40" fill={shade} stroke={stroke} strokeWidth="1.5" />}
      {config.head === 'hex' && (
        <polygon points="53,34 42.5,52.2 21.5,52.2 11,34 21.5,15.8 42.5,15.8" fill={shade} stroke={stroke} strokeWidth="1.5" />
      )}
      {config.head === 'diamond' && (
        <polygon points="32,12 52,34 32,56 12,34" fill={shade} stroke={stroke} strokeWidth="1.5" />
      )}
    </>
  );

  if (sleeping) {
    // Лёжа в кровати: голова на подушке (левый край), тело под одеялом,
    // одеяло свисает на лицевую сторону матраса; y=40 — верх матраса
    return (
      <svg
        width={104 * k}
        height={52 * k}
        viewBox="0 0 104 52"
        role="img"
        aria-label={label}
        className="room-av room-av--sleep"
      >
        <g transform="translate(15 27) rotate(-22) scale(0.6) translate(-32 -34)">
          {headShape}
          {/* Закрытые глаза */}
          <g fill="none" stroke={eyeColor} strokeWidth="2" strokeLinecap="round">
            <path d="M22,37 Q25.5,40.5 29,37" />
            <path d="M35,37 Q38.5,40.5 42,37" />
          </g>
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

  const head = (
    <g transform={headTilt} className={attention === 'glance' ? 'room-av-head room-av-head--glance' : 'room-av-head'}>
      {headShape}
      <g transform={eyeShift}>
        <g className="room-av-eyes" style={blink}>
          <text x="32" y="40" textAnchor="middle" fontSize="9" fill={eyeColor} fontFamily="'JetBrains Mono', monospace">
            {eyeOptions[config.eyes] ?? eyeOptions[0]}
          </text>
        </g>
      </g>
      {attention === 'with_you' && (
        <path d="M28,45 Q32,48 36,45" fill="none" stroke={eyeColor} strokeWidth="1.2" strokeLinecap="round" />
      )}
    </g>
  );

  // Руки (поворот вокруг плеча; «+» у левой и «−» у правой — к центру):
  // читает — обе к книге; за столом — предплечья на столешнице
  const armLen = atDesk ? 13 : seat ? 15 : 19;
  const armL =
    pose === 'read' ? 'rotate(-28 17.5 57)'
    : atDesk ? 'rotate(-16 17.5 57)'
    : seat ? 'rotate(-10 17.5 57)'
    : 'rotate(6 17.5 57)';
  const armR =
    pose === 'read' ? 'rotate(28 46.5 57)'
    : pose === 'write' ? (atDesk ? 'rotate(10 46.5 57)' : 'rotate(34 46.5 57)')
    : atDesk ? 'rotate(16 46.5 57)'
    : seat ? 'rotate(10 46.5 57)'
    : attention === 'with_you' ? 'rotate(-14 46.5 57)'
    : 'rotate(-6 46.5 57)';

  // Раскрытая книга на уровне груди: обложка акцентом, светлые страницы
  const book = pose === 'read' && (
    <g>
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
        <path d="M23,55 L41,55 L44,78 L20,78 Z" fill={shade} stroke={stroke} strokeWidth="1.5" />
        <rect x="15" y="57" width="5" height={armLen} fill={shade} stroke={stroke} strokeWidth="1.2" transform={armL} />
        <rect x="44" y="57" width="5" height={armLen} fill={shade} stroke={stroke} strokeWidth="1.2" transform={armR} />
        {head}
        {book}
      </g>
    </g>
  );

  const legs =
    seat === 'floor' ? (
      // По-турецки: сложенные ноги — широкий низкий блок
      <>
        <rect x="12" y="88" width="40" height="9" rx="3" fill={shade} stroke={stroke} strokeWidth="1.2" />
        <line x1="32" y1="89" x2="32" y2="96" stroke={stroke} strokeWidth="1" />
      </>
    ) : seat ? (
      // На стуле: бёдра к зрителю (короткий широкий блок) и голени
      <>
        <rect x="23" y="89" width="6" height="8" fill={shade} stroke={stroke} strokeWidth="1.2" />
        <rect x="35" y="89" width="6" height="8" fill={shade} stroke={stroke} strokeWidth="1.2" />
        <rect x="19" y="84" width="26" height="6" fill={shade} stroke={stroke} strokeWidth="1.2" />
      </>
    ) : (
      <>
        <rect x="23" y="78" width="7" height="19" fill={shade} stroke={stroke} strokeWidth="1.2" />
        <rect x="34" y="78" width="7" height="19" fill={shade} stroke={stroke} strokeWidth="1.2" />
      </>
    );

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

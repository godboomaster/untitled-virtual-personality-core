import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react';
import { useI18n } from '../i18n';
import Icon from '../components/icons';
import { itemIcon } from '../components/iconChoices';
import type { InventoryItem, RoomAvatarPreset, RoomPropId } from '../mockData';
import type { SkinTimeOfDay, SkinWeather } from '../skins/payloads';
import AvatarFigure from './AvatarFigure';
import { figureAnchor, pickSprite } from './avatarOptions';
import type { AvatarSeat, SpriteLike } from './avatarOptions';
import { acquiredToday, itemPoint, pointStyle, spotPoint, zonePoint } from './roomModel';
import type { Point, SceneConfig } from './roomModel';
import { useDocumentVisible, useMinuteNow, useSeenItems } from './roomHooks';
import type { RoomClientPose, RoomPlan, RoomPose } from './roomTypes';

/* Сцена комнаты: процедурная (CSS-реквизит) или пользовательский фон.
   Полностью управляется пропсами — годится и для раздела «Комната», и для
   компактного PiP-окна (compact). Документ-владелец берётся из ownerDocument
   корневого узла: портал в другое окно работает без правок.

   Производительность: реквизит, часы, питомец, аватар и слой предметов —
   отдельные memo-компоненты; часы тикают раз в минуту (CSS-переменные),
   питомец меняет состояние раз в несколько минут, анимации ставятся на
   паузу, пока документ скрыт (.room-scene--paused). */

export interface RoomSceneNote {
  id: number;
  text: string;
  time: string;
}

export interface RoomSceneProps {
  personaId: string;
  personaName: string;
  config: SceneConfig;
  spotKey: string; // текущее место; 'away' — персоны нет в комнате
  pose: RoomClientPose; // итоговая поза (с учётом клиентских реплик)
  statusLine: ReactNode; // строка статуса поверх сцены
  avatar: RoomAvatarPreset;
  sprite?: SpriteLike | null;
  sprites?: Partial<Record<string, SpriteLike>> | null;
  roomBg?: { dataUrl: string; floorPoints: Partial<Record<string, Point>> } | null;
  items: InventoryItem[];
  itemsReady?: boolean; // список предметов настоящий (онлайн, раскладка пришла) — следы «новое»
  timeOfDay: SkinTimeOfDay;
  weather?: SkinWeather['condition'];
  note?: RoomSceneNote | null; // последнее непрочитанное событие — записка на столе
  nextPlan?: RoomPlan | null;
  compact?: boolean; // PiP: только сцена и одна строка статуса
  editable?: boolean; // перетаскивание/resize меток предметов
  ownerDocument?: Document | null;
  className?: string;
  style?: CSSProperties;
  overlay?: ReactNode; // слот поверх сцены (пузырь реплики, таймер и т.п.)
  onClick?: (e: ReactMouseEvent<HTMLDivElement>) => void;
  onItemChange?: (id: number, patch: Partial<InventoryItem>) => void;
  onPoke?: () => void;
  onNoteOpen?: (id: number) => void;
}

// Реакция на клик по аватару — не чаще раза в 2 минуты на персону
const POKE_COOLDOWN_MS = 2 * 60_000;
const GLANCE_MS = 3500;
const WALK_MS = 2400;
// Смена места в первые секунды после монтирования (демо → живое состояние,
// смена персоны) — без прогулки через комнату, фигура сразу на месте
const SNAP_MS = 3000;
const lastPokeAt = new Map<string, number>();

function RoomSceneImpl(props: RoomSceneProps) {
  const {
    personaId, personaName, config, spotKey, pose, statusLine, avatar, sprite, sprites, roomBg, items,
    itemsReady = true, timeOfDay, weather, note, nextPlan, compact = false, editable = false, ownerDocument, className, style,
    overlay, onClick, onItemChange, onPoke, onNoteOpen,
  } = props;
  const { t } = useI18n();
  const rootRef = useRef<HTMLDivElement>(null);
  const visible = useDocumentVisible(rootRef, ownerDocument);
  const floorPoints = roomBg?.floorPoints ?? null;

  // Локальная реакция на клик по аватару
  const [glancing, setGlancing] = useState(false);
  useEffect(() => {
    if (!glancing) return;
    const timer = setTimeout(() => setGlancing(false), GLANCE_MS);
    return () => clearTimeout(timer);
  }, [glancing]);
  const poke = useCallback(() => {
    const now = Date.now();
    if (now - (lastPokeAt.get(personaId) ?? 0) < POKE_COOLDOWN_MS) return;
    lastPokeAt.set(personaId, now);
    setGlancing(true);
    onPoke?.();
  }, [personaId, onPoke]);

  // Следы: новые предметы подсвечены, пока по ним не кликнули
  const names = useMemo(() => items.map((i) => i.name), [items]);
  const fresh = useMemo(
    () => new Set(items.filter((i) => acquiredToday(i.acquired, Date.now())).map((i) => i.name)),
    [items],
  );
  const { seen, markSeen } = useSeenItems(personaId, names, fresh, itemsReady);

  // Поза тела: реплики (with_you, взгляд) не поднимают персону с места —
  // тело остаётся в последней серверной позе на этом месте (или в позе места
  // по умолчанию), к зрителю поворачивается только лицо. Спящего клик не будит
  const cue: 'with_you' | 'glance' | null =
    pose === 'sleep' || pose === 'away' ? null
    : glancing || pose === 'glance' ? 'glance'
    : pose === 'with_you' ? 'with_you'
    : null;
  const [lastBody, setLastBody] = useState<{ spot: string; pose: RoomPose } | null>(null);
  if (!cue && (lastBody?.spot !== spotKey || lastBody.pose !== pose)) {
    setLastBody({ spot: spotKey, pose: pose as RoomPose });
  }
  const restPose = config.spots.find((s) => s.key === spotKey)?.pose ?? 'stand';
  const cueBody = lastBody?.spot === spotKey ? lastBody.pose : restPose;
  const body: RoomPose = cue ? (cueBody === 'sleep' || cueBody === 'away' ? 'stand' : cueBody) : (pose as RoomPose);

  // Сиденье: за столом (процедурный стол) — стул за столешницей; на фоне
  // пользователя и на кровати — просто сиденье; в остальных местах — пол
  const deskSpot = spotKey === 'desk' || spotKey === 'chair';
  const drawnDesk = !roomBg && config.props.includes('desk');
  const seat: AvatarSeat = deskSpot ? (drawnDesk ? 'desk' : 'chair') : spotKey === 'bed' ? 'chair' : 'floor';

  const point = useMemo(
    () => spotPoint({ spotKey, config, floorPoints, items, pose: body }),
    [spotKey, config, floorPoints, items, body],
  );

  const cls = [
    'room-scene',
    'bracketed',
    `room-scene--tod-${timeOfDay}`,
    weather ? `room-scene--wx-${weather}` : '',
    (timeOfDay === 'evening' || timeOfDay === 'night') && config.props.includes('lamp') ? 'room-scene--lamp-on' : '',
    compact ? 'room-scene--compact' : '',
    // Персона за столом: столешница и вещи на ней — перед фигурой
    deskSpot && drawnDesk ? 'room-scene--at-desk' : '',
    // Спит в кровати: кот перебирается на одеяло в ногах
    !roomBg && spotKey === 'bed' && body === 'sleep' ? 'room-scene--in-bed' : '',
    visible ? '' : 'room-scene--paused',
    className ?? '',
  ].filter(Boolean).join(' ');

  // Предмет на столе процедурной сцены — стабильный объект, чтобы memo реквизита держался
  const d0 = items[0];
  const deskItem = useMemo(
    () => (d0 ? { name: d0.name, icon: d0.icon, image: d0.image } : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [d0?.name, d0?.icon, d0?.image],
  );
  const notePoint = useMemo(() => zonePoint('desk', 'note', floorPoints), [floorPoints]);

  return (
    <div ref={rootRef} className={cls} style={style} onClick={onClick}>
      {!compact && (
        <>
          <div className="corner tl" />
          <div className="corner tr" />
          <div className="corner bl" />
          <div className="corner br" />
        </>
      )}
      <div className="room-status-plate">
        <span className="status-led" />
        <span className="room-status-text">{statusLine}</span>
      </div>
      {!compact && (sprite || roomBg) && <div className="room-art-badge">{t('room.userArt')}</div>}
      {roomBg ? (
        <BgImage src={roomBg.dataUrl} alt={t('room.bgAlt')} />
      ) : (
        <ProceduralBackdrop
          props={config.props}
          posterLabel={config.posterLabel}
          deskItem={deskItem}
          ownerDocument={ownerDocument}
        />
      )}
      {!roomBg && config.pet !== 'none' && (
        <ScenePet kind={config.pet} label={config.petLabel} compact={compact} ownerDocument={ownerDocument} />
      )}
      {(timeOfDay === 'night' || timeOfDay === 'evening') && <div className="room-night-shade" />}
      {note && (
        <button
          type="button"
          className="room-note"
          style={pointStyle(notePoint)}
          title={`${note.time} — ${note.text}`}
          aria-label={t('room.noteAria', { text: note.text })}
          onClick={(e) => {
            e.stopPropagation();
            onNoteOpen?.(note.id);
          }}
        />
      )}
      {!compact && nextPlan?.title && (
        <div className="room-plan-tag" title={nextPlan.title}>
          <span className="room-plan-tag-k">{t('room.planNext')}</span> {nextPlan.title}
        </div>
      )}
      {point && (
        <AvatarLayer
          point={point}
          body={body}
          cue={cue}
          seat={seat}
          avatar={avatar}
          sprite={sprite}
          sprites={sprites}
          seed={personaId}
          name={personaName}
          onPoke={poke}
        />
      )}
      <ItemMarkers
        items={items}
        floorPoints={floorPoints}
        editable={editable && !compact}
        seen={seen}
        rootRef={rootRef}
        onItemChange={onItemChange}
        onSeen={markSeen}
      />
      {overlay}
    </div>
  );
}

const RoomScene = memo(RoomSceneImpl);
export default RoomScene;

// ── Фон пользователя ──

const BgImage = memo(function BgImage({ src, alt }: { src: string; alt: string }) {
  return <img className="room-bg-img" src={src} alt={alt} draggable={false} />;
});

// ── Процедурный реквизит ──

interface BackdropProps {
  props: RoomPropId[];
  posterLabel?: string;
  deskItem: { name: string; icon: string; image?: string } | null;
  ownerDocument?: Document | null;
}

const ProceduralBackdrop = memo(function ProceduralBackdrop({ props, posterLabel, deskItem, ownerDocument }: BackdropProps) {
  const has = (p: RoomPropId) => props.includes(p);
  return (
    <>
      <div className="room-floor" />
      {has('rug') && <div className="room-rug" />}
      {has('garland') && (
        <div className="room-garland">
          <svg width="100%" height="30" viewBox="0 0 100 24" preserveAspectRatio="none">
            <path
              d="M0,4 Q25,22 50,9 Q75,22 100,4"
              fill="none"
              stroke="var(--text-muted)"
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
            />
            <rect x="11" y="10.5" width="2.6" height="2.6" fill="var(--text-dim)" />
            <rect x="30.5" y="15" width="2.6" height="2.6" fill="var(--text-dim)" />
            <rect x="49" y="8.2" width="2.6" height="2.6" fill="var(--accent)" className="room-garland-bulb" />
            <rect x="68.5" y="15" width="2.6" height="2.6" fill="var(--text-dim)" />
            <rect x="86" y="10.5" width="2.6" height="2.6" fill="var(--text-dim)" />
          </svg>
        </div>
      )}
      {has('clock') && <SceneClock ownerDocument={ownerDocument} />}
      {has('poster') && (
        <div className="room-poster">
          <div className="room-poster-frame" />
          <div className="room-poster-label">{posterLabel}</div>
        </div>
      )}
      {has('mirror') && <div className="room-mirror" />}
      {has('secondMirror') && <div className="room-mirror room-mirror--small" />}
      <div className="room-window">
        <div className="room-window-cross-h" />
        <div className="room-window-cross-v" />
      </div>
      {has('curtains') && (
        <>
          <div className="room-curtain room-curtain--l" />
          <div className="room-curtain room-curtain--r" />
        </>
      )}
      {has('shelf') && (
        <div className="room-shelf">
          {[26, 34, 22, 30, 38, 26, 32].map((h, i) => (
            <div key={i} className="room-book" style={{ height: h }} />
          ))}
        </div>
      )}
      {has('shelfLower') && (
        <div className="room-shelf room-shelf--lower">
          {[18, 24, 15, 21].map((h, i) => (
            <div key={i} className="room-book" style={{ height: h }} />
          ))}
        </div>
      )}
      {has('serverRack') && (
        <div className="room-serverrack">
          <div className="room-serverrack-leds" />
          <span className="room-serverrack-live" />
        </div>
      )}
      {has('easel') && (
        <div className="room-easel">
          <div className="room-easel-canvas" />
          <div className="room-easel-tray" />
        </div>
      )}
      {has('frames') && <div className="room-frames" />}
      {has('scrolls') && <div className="room-scrolls" />}
      {has('desk') && (
        <div className="room-desk">
          {deskItem && (
            <span className="room-desk-item" title={deskItem.name}>
              {deskItem.image
                ? <img className="room-item-img" src={deskItem.image} alt={deskItem.name} />
                : <Icon name={itemIcon(deskItem.icon)} size={18} />}
            </span>
          )}
        </div>
      )}
      {has('brushJars') && (
        <div className="room-brushjars">
          <div className="room-jar" />
          <div className="room-jar" />
        </div>
      )}
      {has('candles') && (
        <div className="room-candles">
          <div className="room-candle" />
          <div className="room-candle room-candle--tall" />
        </div>
      )}
      {has('masterTerminal') && (
        <div className="room-terminal">
          <div className="room-terminal-screen" />
        </div>
      )}
      {has('chair') && <div className="room-chair" />}
      {has('bed') && <div className="room-bed" />}
      {has('lamp') && (
        <div className="room-lamp">
          <div className="room-lamp-cone" />
          <div className="room-lamp-head" />
          <div className="room-lamp-pole" />
          <div className="room-lamp-base" />
        </div>
      )}
      {has('plant') && (
        <div className="room-plant">
          <div className="room-plant-leaf room-plant-leaf--l" />
          <div className="room-plant-leaf room-plant-leaf--c" />
          <div className="room-plant-leaf room-plant-leaf--r" />
          <div className="room-plant-pot" />
        </div>
      )}
    </>
  );
});

// Настоящие часы: стрелки из локального времени через CSS-переменные,
// обновление раз в минуту; секундная — CSS-вращение 60 с со сдвигом фазы
function SceneClock({ ownerDocument }: { ownerDocument?: Document | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const now = useMinuteNow(ref, ownerDocument);
  const d = new Date(now);
  const h = d.getHours();
  const m = d.getMinutes();
  const vars = {
    '--room-clock-h': `${(h % 12) * 30 + m * 0.5}deg`,
    '--room-clock-m': `${m * 6}deg`,
  } as CSSProperties;
  // Фаза секундной стрелки пересчитывается на каждом минутном тике (key),
  // после паузы в скрытой вкладке стрелка не отстаёт
  const sec = Math.floor((Date.now() % 60_000) / 1000);
  return (
    <div ref={ref} className="room-clock" style={vars}>
      <div className="room-clock-hand room-clock-hand--h" />
      <div className="room-clock-hand room-clock-hand--m" />
      <div key={now} className="room-clock-hand room-clock-hand--s" style={{ animationDelay: `-${sec}s` }} />
    </div>
  );
}

// ── Питомец: свой медленный цикл (спит / не спит / умывается) ──

type PetState = 'sleep' | 'awake' | 'groom';

function nextPetState(cur: PetState): PetState {
  const r = Math.random();
  if (cur === 'sleep') return r < 0.6 ? 'awake' : 'groom';
  if (cur === 'awake') return r < 0.55 ? 'sleep' : 'groom';
  return r < 0.5 ? 'sleep' : 'awake';
}

function ScenePet({
  kind,
  label,
  compact,
  ownerDocument,
}: {
  kind: 'cat' | 'crow';
  label?: string;
  compact: boolean;
  ownerDocument?: Document | null;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<PetState>('sleep');
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const doc = ownerDocument ?? ref.current?.ownerDocument ?? null;
    // Одна setTimeout-цепочка, шаг 3–6 минут; в скрытом документе состояние не меняем
    const step = () => {
      if (!doc?.hidden) setState((s) => nextPetState(s));
      timer = setTimeout(step, (3 + Math.random() * 3) * 60_000);
    };
    timer = setTimeout(step, (3 + Math.random() * 3) * 60_000);
    return () => clearTimeout(timer);
  }, [ownerDocument]);

  // Подпись из конфига — до « · », состояние добавляем сами
  const base = (label ?? '').split(' · ')[0];
  const fullLabel = base ? `${base} · ${t(`room.pet.${state}`)}` : t(`room.pet.${state}`);

  if (kind === 'cat') {
    return (
      <>
        <div ref={ref} className={`room-cat room-cat--${state}`} title={fullLabel}>
          <svg width="56" height="28" viewBox="0 0 56 28" role="img" aria-label={t('room.catAria')}>
            <ellipse cx="24" cy="21" rx="16" ry="6.5" fill="var(--text-dim)" />
            {state === 'awake' ? (
              <>
                <circle cx="42" cy="12" r="6.5" fill="var(--text-dim)" />
                <polygon points="38,8 39.5,2 42,6" fill="var(--text-dim)" />
                <polygon points="44,6 47,2 48,8" fill="var(--text-dim)" />
                <circle cx="40.5" cy="12" r="1" fill="var(--bg-panel)" />
                <circle cx="44.5" cy="12" r="1" fill="var(--bg-panel)" />
                <path d="M9 21 q-8 -4 -5 -15" stroke="var(--text-dim)" strokeWidth="3" fill="none" strokeLinecap="round" className="room-cat-tail" />
              </>
            ) : state === 'groom' ? (
              <>
                <circle cx="40" cy="18" r="6.5" fill="var(--text-dim)" />
                <polygon points="35,14 36.5,8 39,12" fill="var(--text-dim)" />
                <polygon points="41,12 44,8 45,14" fill="var(--text-dim)" />
                <ellipse cx="46" cy="21" rx="3" ry="2" fill="var(--text-muted)" />
                <path d="M9 21 q-7 -1 -6 -9" stroke="var(--text-dim)" strokeWidth="3" fill="none" strokeLinecap="round" />
              </>
            ) : (
              <>
                <circle cx="42" cy="16" r="6.5" fill="var(--text-dim)" />
                <polygon points="38,12 39.5,6 42,10" fill="var(--text-dim)" />
                <polygon points="44,10 47,6 48,12" fill="var(--text-dim)" />
                <path d="M9 21 q-7 -1 -6 -9" stroke="var(--text-dim)" strokeWidth="3" fill="none" strokeLinecap="round" />
                <line x1="43" y1="16" x2="46" y2="16" stroke="var(--bg-panel)" strokeWidth="1.2" />
              </>
            )}
          </svg>
          {state === 'sleep' && <span className="room-cat-z">z z</span>}
        </div>
        {!compact && <div className="room-cat-label">{fullLabel}</div>}
      </>
    );
  }
  return (
    <>
      <div ref={ref} className={`room-crow room-crow--${state}`} title={fullLabel}>
        <svg width="44" height="28" viewBox="0 0 44 28" role="img" aria-label={t('room.crowAria')}>
          <polygon points="7,14 0,10 6,18" fill="var(--text-dim)" />
          <ellipse cx="18" cy="16" rx="11" ry="6" fill="var(--text-dim)" />
          {state === 'sleep' ? (
            <>
              <circle cx="27" cy="13" r="5" fill="var(--text-dim)" />
              <line x1="27" y1="12" x2="30" y2="12" stroke="var(--bg-panel)" strokeWidth="0.9" />
            </>
          ) : state === 'groom' ? (
            <>
              <circle cx="24" cy="10" r="5" fill="var(--text-dim)" />
              <polygon points="20,10 13,12 20,13.5" fill="var(--text-dim)" />
              <circle cx="22.5" cy="9" r="0.9" fill="var(--bg-panel)" />
            </>
          ) : (
            <>
              <circle cx="30" cy="11" r="5" fill="var(--text-dim)" />
              <polygon points="34,10 41,12 34,13.5" fill="var(--text-dim)" />
              <circle cx="31.5" cy="10" r="0.9" fill="var(--bg-panel)" />
            </>
          )}
          <line x1="15" y1="21" x2="15" y2="26" stroke="var(--text-dim)" strokeWidth="1.5" />
          <line x1="21" y1="21" x2="21" y2="26" stroke="var(--text-dim)" strokeWidth="1.5" />
        </svg>
      </div>
      {!compact && <div className="room-crow-label">{fullLabel}</div>}
    </>
  );
}

// ── Аватар: переход между местами (CSS transition left/top) ──

interface AvatarLayerProps {
  point: Point;
  body: RoomPose;
  cue: 'with_you' | 'glance' | null;
  seat: AvatarSeat;
  avatar: RoomAvatarPreset;
  sprite?: SpriteLike | null;
  sprites?: Partial<Record<string, SpriteLike>> | null;
  seed: string;
  name: string;
  onPoke: () => void;
}

const AvatarLayer = memo(function AvatarLayer({ point, body, cue, seat, avatar, sprite, sprites, seed, name, onPoke }: AvatarLayerProps) {
  const { t } = useI18n();
  const anchorRef = useRef<HTMLDivElement>(null);
  const mountedAt = useRef(0);
  useEffect(() => {
    mountedAt.current = Date.now();
  }, []);
  // Пока идёт переход — фигура стоит и смотрит по ходу движения
  const prev = useRef(point);
  const [walk, setWalk] = useState<{ facing: 1 | -1 } | null>(null);
  useLayoutEffect(() => {
    const from = prev.current;
    prev.current = point;
    if (Math.abs(from.x - point.x) < 0.005 && Math.abs(from.y - point.y) < 0.005) return;
    if (Date.now() - mountedAt.current < SNAP_MS) {
      // До отрисовки: переход выключен на один пересчёт стилей
      const el = anchorRef.current;
      if (el) {
        el.style.transition = 'none';
        el.getBoundingClientRect();
        el.style.transition = '';
      }
      return;
    }
    setWalk({ facing: point.x < from.x ? -1 : 1 });
    const timer = setTimeout(() => setWalk(null), WALK_MS);
    return () => clearTimeout(timer);
  }, [point]);

  const shownBody: RoomPose = walk ? 'stand' : body;
  const shownCue = walk ? null : cue;
  const picked = pickSprite(shownCue ?? shownBody, sprite, sprites);
  const anchor = picked ? picked.asset.anchor : figureAnchor(shownBody);

  return (
    <div
      ref={anchorRef}
      className={`room-av-anchor${walk ? ' room-av-anchor--walking' : ''}`}
      style={pointStyle(point)}
    >
      <div
        className="room-av-shift"
        style={{ transform: `translate(${-anchor.x * 100}%, ${-anchor.y * 100}%)` }}
      >
        <div
          className="room-av-hit"
          role="button"
          tabIndex={0}
          title={t('room.pokeTitle', { name })}
          aria-label={t('room.pokeTitle', { name })}
          style={walk?.facing === -1 ? { transform: 'scaleX(-1)' } : undefined}
          onClick={(e) => {
            e.stopPropagation();
            onPoke();
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onPoke();
            }
          }}
        >
          {picked ? (
            <SpriteImg
              src={picked.asset.dataUrl}
              pose={picked.own ? null : (shownCue ?? shownBody)}
              anchor={picked.asset.anchor}
              alt={t('room.spriteAlt')}
            />
          ) : (
            <AvatarFigure config={avatar} pose={shownBody} attention={shownCue} seat={seat} height={132} seed={seed} />
          )}
        </div>
      </div>
    </div>
  );
});

// Спрайт: смена позы у базового спрайта — CSS-модификатор (без перезагрузки картинки)
const SpriteImg = memo(function SpriteImg({
  src,
  pose,
  anchor,
  alt,
}: {
  src: string;
  pose: RoomClientPose | null;
  anchor: { x: number; y: number };
  alt: string;
}) {
  return (
    <img
      className={`room-sprite-img${pose && pose !== 'stand' ? ` room-sprite--${pose}` : ''}`}
      style={{ transformOrigin: `${anchor.x * 100}% ${anchor.y * 100}%` }}
      src={src}
      alt={alt}
      draggable={false}
    />
  );
});

// ── Слой предметов: метки (пользовательские или авто по зоне LLM) ──

interface ItemMarkersProps {
  items: InventoryItem[];
  floorPoints: Partial<Record<string, Point>> | null;
  editable: boolean;
  seen: ReadonlySet<string> | null;
  rootRef: React.RefObject<HTMLDivElement | null>;
  onItemChange?: (id: number, patch: Partial<InventoryItem>) => void;
  onSeen: (name: string) => void;
}

const ItemMarkers = memo(function ItemMarkers({ items, floorPoints, editable, seen, rootRef, onItemChange, onSeen }: ItemMarkersProps) {
  const { t } = useI18n();
  const dragItemId = useRef<number | null>(null);
  const resizeState = useRef<{ id: number; startX: number; startSize: number } | null>(null);

  // Точка указателя в долях сцены (getBoundingClientRect учитывает scale фулскрина)
  const fraction = (e: ReactPointerEvent) => {
    const sc = rootRef.current;
    if (!sc) return null;
    const r = sc.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
      y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
      width: r.width,
    };
  };

  const onItemDown = (item: InventoryItem) => (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation(); // клик по предмету не должен закрывать модальный режим
    onSeen(item.name);
    if (!editable || !onItemChange) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    // Метка ставится только при движении: простой клик (снять подсветку
    // «новое») не превращает авто-метку зоны LLM в пользовательскую и не шлёт PUT
    dragItemId.current = item.id;
  };
  const onResizeDown = (item: InventoryItem) => (e: ReactPointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    if (!editable) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    resizeState.current = { id: item.id, startX: e.clientX, startSize: item.size ?? 6 };
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!onItemChange) return;
    if (dragItemId.current != null) {
      const p = fraction(e);
      if (p) onItemChange(dragItemId.current, { marker: { x: p.x, y: p.y } });
    } else if (resizeState.current) {
      const p = fraction(e);
      if (!p) return;
      const { id, startX, startSize } = resizeState.current;
      // Размер — % ширины сцены: дельта указателя переводится в проценты
      const next = Math.max(2, Math.min(25, startSize + ((e.clientX - startX) / p.width) * 100));
      onItemChange(id, { size: Math.round(next * 10) / 10 });
    }
  };
  const onUp = () => {
    dragItemId.current = null;
    resizeState.current = null;
  };

  return (
    <div className="room-item-marker-layer">
      {items.map((i) => {
        const p = itemPoint(i, floorPoints);
        if (!p) return null;
        const isNew = seen != null && !seen.has(i.name);
        return (
          <div
            key={i.id}
            className={`room-item-marker${isNew ? ' room-item-marker--new' : ''}${editable ? '' : ' room-item-marker--static'}${i.marker ? '' : ' room-item-marker--auto'}`}
            style={{ ...pointStyle(p), width: `${i.size ?? 6}%` }}
            title={isNew ? `${i.name} · ${t('room.itemNew')}` : i.name}
            onPointerDown={onItemDown(i)}
            onPointerMove={onMove}
            onPointerUp={onUp}
          >
            {i.image
              ? <img className="room-item-marker-img" src={i.image} alt={i.name} draggable={false} />
              : <Icon name={itemIcon(i.icon)} size={22} />}
            {editable && (
              <div
                className="room-item-resize"
                onPointerDown={onResizeDown(i)}
                onPointerMove={onMove}
                onPointerUp={onUp}
              />
            )}
          </div>
        );
      })}
    </div>
  );
});

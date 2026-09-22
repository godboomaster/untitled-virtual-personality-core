import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { InventoryItem, RoomAvatarPreset, RoomPastime, RoomPropId } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { usePersonaLivingState } from '../apiData';
import { usePersonaArt } from '../artStore';
import ArtPanel from '../components/ArtPanel';
import ItemPlacer from '../components/ItemPlacer';
import RoomEditor from '../components/RoomEditor';
import Icon from '../components/icons';
import { ICON_CHOICES, itemIcon } from '../components/iconChoices';
import { setPersonaItems, usePersonaItems } from '../inventoryStore';
import SkinFrame from '../components/SkinFrame';
import { usePersonaSkin } from '../skins/skinStore';
import { buildRoomPayload } from '../skins/payloads';
import { useShellTheme } from '../skins/shellTheme';

/* Раздел «Комната» — личное пространство персоны. У каждой персоны своя
   комната: обстановка, аватар, инвентарь и лента активности берутся из
   конфигов в mockData (roomConfigs, inventoryByPersona, activitiesByPersona). */

type AvatarConfig = RoomAvatarPreset;

const headShapes: { id: AvatarConfig['head']; glyph: string }[] = [
  { id: 'circle', glyph: '●' },
  { id: 'square', glyph: '■' },
  { id: 'hex', glyph: '⬡' },
  { id: 'diamond', glyph: '◆' },
];

const eyeOptions = ['◉ ◉', '▪ ▪', '— —', '◠ ◠', '✦ ✦'];

const accessoryOptions = ['antenna', 'ears', 'halo', 'none'] as const;

// Оттенки серого для корпуса аватара
const avatarShades = ['#1c1c1c', '#4a4a4a', '#8f8f8f', '#c9c9c9'];

// Иконки на выбор для предметов инвентаря (ICON_CHOICES, itemIcon) —
// в components/icons, общие с разделом «Редактор»

// SVG-фигура аватара: голова выбранной формы + глаза-глифы + аксессуар
function AvatarFigure({ config, size = 64 }: { config: AvatarConfig; size?: number }) {
  const { t } = useI18n();
  const shade = avatarShades[config.shade];
  // На светлых оттенках глаза тёмные, на тёмных — светлые
  const eyeColor = config.shade >= 2 ? '#1c1c1c' : '#eae8e1';
  const stroke = config.shade >= 2 ? '#5a5a5a' : '#8f8f8f';
  const accessory = accessoryOptions[config.accessory];

  return (
    <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={t('room.avatarAria')}>
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
      <text
        x="32"
        y="40"
        textAnchor="middle"
        fontSize="9"
        fill={eyeColor}
        fontFamily="'JetBrains Mono', monospace"
      >
        {eyeOptions[config.eyes]}
      </text>
    </svg>
  );
}

export default function Room() {
  const { t } = useI18n();
  const { personas, inventoryByPersona, activitiesByPersona, roomConfigs } = useMockData();
  // Текущая выбранная персона и её конфиг комнаты
  const [personaId, setPersonaId] = useState(() => personas[0].id);
  const persona = personas.find((p) => p.id === personaId) ?? personas[0];
  const cfg = roomConfigs[persona.id] ?? roomConfigs.connor;
  // Живое состояние персоны с бэкенда (ui_room_mood_sync, слой state/world):
  // перекрывает моковые телеметрические значения и ленту «пока тебя не было»
  const living = usePersonaLivingState(persona.id);
  const liveEnergy = living ? `${Math.round(living.state!.energy)}%` : cfg.energy;
  const liveMood = living ? living.state!.mood.tag : cfg.mood;
  const livePastime = living ? living.state!.pastime : null;
  const liveLocation = living ? living.state!.location : null;
  // Пользовательский арт персоны (спрайт / фон комнаты) из арт-мастерской
  const art = usePersonaArt(persona.id);

  // Применённые аватары всех персон (правки конструктора живут в рамках сессии)
  const [appliedByPersona, setAppliedByPersona] = useState<Record<string, AvatarConfig>>(() =>
    Object.fromEntries(Object.entries(roomConfigs).map(([id, c]) => [id, { ...c.avatar }])),
  );
  const applied = appliedByPersona[persona.id] ?? roomConfigs.connor.avatar;
  const [draft, setDraft] = useState<AvatarConfig>(applied);

  // Инвентарь: общий стор правок (разделы «Комната» и «Редактор» видят одни
  // данные); пока правок нет — моковый список текущей локали
  const itemsOverride = usePersonaItems(persona.id);
  const items = itemsOverride ?? inventoryByPersona[persona.id] ?? [];
  const setItems = (next: InventoryItem[]) => setPersonaItems(persona.id, next);
  // Редактор предмета: черновик + признак «новый предмет» (против правки существующего)
  const [itemDraft, setItemDraft] = useState<InventoryItem | null>(null);
  const [itemIsNew, setItemIsNew] = useState(false);

  // Текущее занятие персоны в сцене
  const [activityIdx, setActivityIdx] = useState(0);
  // Полноэкранный режим сцены: только комната поверх всего интерфейса
  const [sceneFullscreen, setSceneFullscreen] = useState(false);
  // Масштаб элементов сцены в фулскрине: px-размеры растут пропорционально
  // высоте экрана (базовая высота сцены в обычном режиме — 430px)
  const [sceneScale, setSceneScale] = useState(1);
  useEffect(() => {
    if (!sceneFullscreen) return;
    // Масштаб под высоту модального окна (70vh), база — 430px высоты сцены
    const update = () => setSceneScale((window.innerHeight * 0.7) / 430);
    update();
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, [sceneFullscreen]);

  // Фулскрин — «одиночная страница»: блокируем прокрутку основного контента
  useEffect(() => {
    if (!sceneFullscreen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, [sceneFullscreen]);
  // Места в комнате: базовые точки из конфига + предметы инвентаря, у которых
  // есть метка и включён флаг spot. Названия занятий для предметов —
  // плейсхолдеры, позже их будет придумывать LLM.
  // key у предметных мест формальный ('desk'): позиция берётся из метки предмета
  type Spot = RoomPastime & { itemId?: number };
  const spots: Spot[] = [
    ...cfg.pastimes,
    ...items
      .filter((i) => i.marker && i.spot)
      .map((i) => ({
        key: 'desk' as const,
        label: t('room.itemPastime', { name: i.name }),
        place: i.name,
        duration: t('room.itemPastimeDuration'),
        itemId: i.id,
      })),
  ];
  const pastime = spots[activityIdx % spots.length];
  // Если занятие — предметное место, позиция аватара = метка предмета на сцене
  const spotItem = pastime.itemId != null ? items.find((i) => i.id === pastime.itemId) : undefined;
  const spotPos = spotItem?.marker ?? null;
  const roomBg = art?.roomBg;
  const sprite = art?.sprite;

  // При смене персоны: черновик = применённый аватар новой персоны, занятие — с начала цикла
  useEffect(() => {
    setDraft(appliedByPersona[persona.id] ?? roomConfigs.connor.avatar);
    setActivityIdx(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona.id]);

  // Цикл занятий: каждые 8 секунд персона перемещается и меняет подпись
  useEffect(() => {
    const timer = setInterval(() => {
      setActivityIdx((i) => (i + 1) % spots.length);
    }, 8000);
    return () => clearInterval(timer);
  }, [persona.id, spots.length]);

  const has = (p: RoomPropId) => cfg.props.includes(p);

  const addItemWith = (name: string, icon: string) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setItems([...items, { id: Date.now(), icon, name: trimmed, description: t('room.addedByOperator'), tag: 'gift' }]);
  };

  // Редактор предмета: открытие (новый / правка), сохранение, удаление
  const openNewItem = () => {
    setItemDraft({ id: Date.now(), icon: 'book', name: '', description: '', tag: 'gift' });
    setItemIsNew(true);
  };
  const openEditItem = (item: InventoryItem) => {
    setItemDraft({ ...item });
    setItemIsNew(false);
  };
  const saveItem = () => {
    if (!itemDraft || !itemDraft.name.trim()) return;
    const draft = { ...itemDraft, name: itemDraft.name.trim() };
    setItems(itemIsNew ? [...items, draft] : items.map((i) => (i.id === draft.id ? draft : i)));
    setItemDraft(null);
  };
  const deleteItem = (id: number) => {
    setItems(items.filter((i) => i.id !== id));
    if (itemDraft?.id === id) setItemDraft(null);
  };
  // Загрузка собственного ассета предмета (dataURL заменяет иконку)
  const pickItemImage = (f: File) => {
    if (!f.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = () => setItemDraft((d) => (d ? { ...d, image: String(reader.result) } : d));
    reader.readAsDataURL(f);
  };
  // Скрытый file input ассета предмета; отдельный редактор размещения на фоне
  const itemImageInputRef = useRef<HTMLInputElement>(null);
  const [placerOpen, setPlacerOpen] = useState(false);
  // Редактор размещения предметов — всплывающее окно поверх раздела
  const [editorOpen, setEditorOpen] = useState(false);

  // Закрытие редактора по Esc
  useEffect(() => {
    if (!editorOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setEditorOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editorOpen]);

  // Перетаскивание и resize предметов прямо на сцене комнаты
  const sceneRef = useRef<HTMLDivElement>(null);
  const dragItemId = useRef<number | null>(null);
  const resizeState = useRef<{ id: number; startX: number; startSize: number } | null>(null);

  const updateItem = (id: number, patch: Partial<InventoryItem>) =>
    setItems(items.map((i) => (i.id === id ? { ...i, ...patch } : i)));

  // Точка указателя в долях сцены (getBoundingClientRect учитывает scale фулскрина)
  const sceneFraction = (e: React.PointerEvent) => {
    const sc = sceneRef.current;
    if (!sc) return null;
    const r = sc.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
      y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
      width: r.width,
    };
  };

  const onItemDown = (id: number) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation(); // клик по предмету не должен закрывать модальный режим
    e.currentTarget.setPointerCapture(e.pointerId);
    dragItemId.current = id;
    const p = sceneFraction(e);
    if (p) updateItem(id, { marker: { x: p.x, y: p.y } });
  };
  const onResizeDown = (item: InventoryItem) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    resizeState.current = { id: item.id, startX: e.clientX, startSize: item.size ?? 6 };
  };
  const onItemPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragItemId.current != null) {
      const p = sceneFraction(e);
      if (p) updateItem(dragItemId.current, { marker: { x: p.x, y: p.y } });
    } else if (resizeState.current) {
      const p = sceneFraction(e);
      if (!p) return;
      const { id, startX, startSize } = resizeState.current;
      // Размер — % ширины сцены: дельта указателя переводится в проценты
      const next = Math.max(2, Math.min(25, startSize + ((e.clientX - startX) / p.width) * 100));
      updateItem(id, { size: Math.round(next * 10) / 10 });
    }
  };
  const onItemPointerUp = () => {
    dragItemId.current = null;
    resizeState.current = null;
  };

  // Лента «пока тебя не было»: недавние события офлайн-жизни (world/state)
  // независимо от consumed — после дневника/инициативы факты остаются
  // видимыми (приглушённо), а не схлопываются в моки. Моки — только когда
  // событий нет вовсе или бэкенд недоступен
  const feed = useMemo(() => {
    const events = living?.recent_events?.length
      ? living.recent_events
      : living?.last_events ?? [];
    if (!events.length) return activitiesByPersona[persona.id] ?? [];
    return events
      .slice()
      .reverse()
      .map((e) => ({
        id: e.id,
        time: (e.timestamp || '').slice(11, 16) || e.timestamp,
        text: e.payload?.event ?? e.payload?.content ?? '',
        dim: e.consumed === true,
      }))
      .filter((a) => a.text);
  }, [living, persona.id]);

  // Скин персоны: если файл комнаты задан и не сломан — сцена, телеметрия,
  // лента и инвентарь рендерятся внутри sandboxed iframe (арт-мастерская остаётся)
  const { skins, broken, reportBroken, reset } = usePersonaSkin(persona.id);
  const roomSkin = broken.room ? null : (skins.room ?? null);
  const skinActive = roomSkin != null;
  // Пока скин активен, каркас приложения (сайдбар, топбар) красится в его палитру
  useShellTheme(roomSkin);
  const skinRoomState = buildRoomPayload({
    persona,
    statusText: t(`status.${persona.status}`),
    pastimeLabel: livePastime ?? pastime.label,
    pastimePlace: liveLocation ?? pastime.place,
    duration: pastime.duration,
    x: spotPos ? spotPos.x * 100 : roomBg ? roomBg.floorPoints[pastime.key].x * 100 : cfg.points[pastime.key],
    y: spotPos ? spotPos.y * 100 : roomBg ? roomBg.floorPoints[pastime.key].y * 100 : null,
    mood: liveMood,
    energy: liveEnergy,
    pet: cfg.pet,
    petLabel: cfg.petLabel,
    bg: roomBg?.dataUrl ?? null,
    sprite: sprite?.dataUrl ?? null,
    feed,
    inventory: items,
  });

  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h2 className="section-title">{t('nav.room')}</h2>
          <p className="section-subtitle">
            {t('room.subtitle')}
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {!skinActive && (
            <button
              type="button"
              className="btn btn--ghost"
              title={t('room.sceneFullscreen')}
              onClick={() => setSceneFullscreen(true)}
            >
              <Icon name="fullscreen" size={13} />
            </button>
          )}
          <span className="badge badge--active">{persona.name} · {t(`status.${persona.status}`)}</span>
        </div>
      </div>

      {/* Переключатель персон: у каждой своя комната */}
      <div className="tabs room-persona-tabs">
        {personas.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`tab ${p.id === persona.id ? 'tab--active' : ''}`}
            onClick={() => setPersonaId(p.id)}
          >
            {p.name}
          </button>
        ))}
      </div>

      {/* Откат при сломанном скине комнаты: дефолтный вид + объяснение */}
      {broken.room && (
        <div className="skin-error-banner">
          <span>{t('skin.brokenBanner', { msg: broken.room })}</span>
          <button type="button" className="btn btn--ghost" onClick={reset}>
            {t('skin.reset')}
          </button>
        </div>
      )}

      {/* Скин персоны заменяет сцену и телеметрию целиком */}
      {skinActive ? (
        <SkinFrame
          className="skin-frame skin-frame--room"
          skin={roomSkin}
          screen="room"
          state={skinRoomState}
          onAction={(action, values) => {
            // Действия записи из скина комнаты (whitelist)
            if (action === 'add-inventory-item' && values.name?.trim()) {
              addItemWith(values.name, values.icon?.trim() || 'book');
            }
          }}
          onError={(m) => reportBroken('room', m)}
          title={t('skin.frameTitle')}
        />
      ) : (
      <div className="room-layout">
        {/* Сцена комнаты. В фулскрине уходит порталом в <body>: анимации-предки
            (section-enter, stagger-item) держат transform и ломают position: fixed —
            без портала оверлей прокручивался вместе со страницей */}
        {(() => {
          const sceneCard = (
        <div
          className={`card room-scene-card stagger-item ${sceneFullscreen ? 'room-scene-card--fullscreen' : ''}`}
          onClick={sceneFullscreen ? () => setSceneFullscreen(false) : undefined}
        >
          {/* Обычный режим: кнопка разворота — в шапке раздела, не на сцене */}
          {/* Модальный режим: рамка в стиле темы — шапка с заголовком и уголки.
              По размеру совпадает с визуальным окном сцены (74vw/1024px × 70vh) */}
          {sceneFullscreen && (
            <div className="room-modal-frame">
              <div className="room-modal-head" onClick={(e) => e.stopPropagation()}>
                <div className="corner tl" />
                <div className="corner tr" />
                <span className="room-modal-title">
                  {persona.name} · {pastime.label}
                </span>
                <span className="badge">ROOM // LIVE</span>
                <button
                  type="button"
                  className="pxe-close"
                  title={t('room.sceneExitFullscreen')}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSceneFullscreen(false);
                  }}
                >
                  ✕
                </button>
              </div>
              <div className="corner bl" />
              <div className="corner br" />
            </div>
          )}
          <div
            ref={sceneRef}
            className="room-scene bracketed"
            onClick={sceneFullscreen ? (e) => e.stopPropagation() : undefined}
            style={
              sceneFullscreen
                ? {
                    // Немасштабированный бокс = целевой размер окна / k;
                    // после scale(k) от центра сцена занимает ровно 74vw × 70vh
                    transform: `scale(${sceneScale})`,
                    width: `${Math.min(window.innerWidth * 0.74, 1024) / sceneScale}px`,
                    height: '430px',
                  }
                : undefined
            }
          >
            <div className="corner tl" />
            <div className="corner tr" />
            <div className="corner bl" />
            <div className="corner br" />
            <div className="room-status-plate">
              <span className="status-led" />
              <span>
                {persona.name} · <span className="val">{pastime.label}</span> · {pastime.duration}
              </span>
            </div>
            {(sprite || roomBg) && <div className="room-art-badge">{t('room.userArt')}</div>}
            {roomBg ? (
              // Пользовательский фон комнаты заменяет процедурную сцену
              <img className="room-bg-img" src={roomBg.dataUrl} alt={t('room.bgAlt')} />
            ) : (
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
            {has('clock') && (
              <div className="room-clock">
                <div className="room-clock-hand room-clock-hand--h" />
                <div className="room-clock-hand room-clock-hand--m" />
                <div className="room-clock-hand room-clock-hand--s" />
              </div>
            )}
            {has('poster') && (
              <div className="room-poster">
                <div className="room-poster-frame" />
                <div className="room-poster-label">{cfg.posterLabel}</div>
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
                {items[0] && (
                  <span className="room-desk-item" title={items[0].name}>
                    {items[0].image
                      ? <img className="room-item-img" src={items[0].image} alt={items[0].name} />
                      : <Icon name={itemIcon(items[0].icon)} size={18} />}
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
            {cfg.pet === 'cat' && (
              <>
                <div className="room-cat" title={cfg.petLabel}>
                  <svg width="56" height="28" viewBox="0 0 56 28" role="img" aria-label={t('room.catAria')}>
                    <ellipse cx="24" cy="21" rx="16" ry="6.5" fill="var(--text-dim)" />
                    <circle cx="42" cy="16" r="6.5" fill="var(--text-dim)" />
                    <polygon points="38,12 39.5,6 42,10" fill="var(--text-dim)" />
                    <polygon points="44,10 47,6 48,12" fill="var(--text-dim)" />
                    <path d="M9 21 q-7 -1 -6 -9" stroke="var(--text-dim)" strokeWidth="3" fill="none" strokeLinecap="round" />
                    <line x1="43" y1="16" x2="46" y2="16" stroke="var(--bg-panel)" strokeWidth="1.2" />
                  </svg>
                  <span className="room-cat-z">z z</span>
                </div>
                <div className="room-cat-label">{cfg.petLabel}</div>
              </>
            )}
            {cfg.pet === 'crow' && (
              <>
                <div className="room-crow" title={cfg.petLabel}>
                  <svg width="44" height="28" viewBox="0 0 44 28" role="img" aria-label={t('room.crowAria')}>
                    <polygon points="7,14 0,10 6,18" fill="var(--text-dim)" />
                    <ellipse cx="18" cy="16" rx="11" ry="6" fill="var(--text-dim)" />
                    <circle cx="30" cy="11" r="5" fill="var(--text-dim)" />
                    <polygon points="34,10 41,12 34,13.5" fill="var(--text-dim)" />
                    <line x1="15" y1="21" x2="15" y2="26" stroke="var(--text-dim)" strokeWidth="1.5" />
                    <line x1="21" y1="21" x2="21" y2="26" stroke="var(--text-dim)" strokeWidth="1.5" />
                    <circle cx="31.5" cy="10" r="0.9" fill="var(--bg-panel)" />
                  </svg>
                </div>
                <div className="room-crow-label">{cfg.petLabel}</div>
              </>
            )}
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
            {!spotPos && (
            <div className="room-avatar-wrap" style={{ left: `${cfg.points[pastime.key]}%` }}>
              {sprite ? (
                <div className="room-avatar">
                  <img className="room-sprite-img" src={sprite.dataUrl} alt={t('room.spriteAlt')} />
                </div>
              ) : (
                <>
                  <div className="room-avatar">
                    <AvatarFigure config={applied} size={110} />
                  </div>
                  <div className="room-avatar-shadow" />
                </>
              )}
            </div>
            )}
              </>
            )}
            {/* Позиция аватара: предметное место (метка предмета) в приоритете,
                иначе — откалиброванные точки пола пользовательского фона;
                якорь-ступни прижимают спрайт к точке */}
            {(() => {
              const avatarPoint = spotPos ?? (roomBg ? roomBg.floorPoints[pastime.key] : null);
              if (!avatarPoint) return null;
              return (
                <div
                  className="room-avatar-wrap room-avatar-wrap--point"
                  style={{ left: `${avatarPoint.x * 100}%`, top: `${avatarPoint.y * 100}%` }}
                >
                  <div
                    className="room-anchor-shift"
                    style={{
                      transform: sprite
                        ? `translate(${-sprite.anchor.x * 100}%, ${-sprite.anchor.y * 100}%)`
                        : 'translate(-50%, -100%)',
                    }}
                  >
                    <div className="room-avatar">
                      {sprite ? (
                        <img className="room-sprite-img" src={sprite.dataUrl} alt={t('room.spriteAlt')} />
                      ) : (
                        <AvatarFigure config={applied} size={110} />
                      )}
                    </div>
                  </div>
                </div>
              );
            })()}
            {/* Предметы инвентаря с метками: стоят на фоне в точке метки,
                таскаются мышью, размер — уголок в правом нижнем углу */}
            <div className="room-item-marker-layer">
              {items
                .filter((i) => i.marker)
                .map((i) => (
                  <div
                    key={i.id}
                    className="room-item-marker"
                    style={{ left: `${i.marker!.x * 100}%`, top: `${i.marker!.y * 100}%`, width: `${i.size ?? 6}%` }}
                    title={i.name}
                    onPointerDown={onItemDown(i.id)}
                    onPointerMove={onItemPointerMove}
                    onPointerUp={onItemPointerUp}
                  >
                    {i.image
                      ? <img className="room-item-marker-img" src={i.image} alt={i.name} draggable={false} />
                      : <Icon name={itemIcon(i.icon)} size={22} />}
                    <div
                      className="room-item-resize"
                      onPointerDown={onResizeDown(i)}
                      onPointerMove={onItemPointerMove}
                      onPointerUp={onItemPointerUp}
                    />
                  </div>
                ))}
            </div>
          </div>
        </div>
          );
          return sceneFullscreen ? createPortal(sceneCard, document.body) : sceneCard;
        })()}

        <div className="room-side">
          {/* Панель состояния: не просто метки, а живые виджеты —
              энергия с баром, настроение + последнее событие ленты,
              переключение занятия и телепорт по точкам комнаты */}
          <div className="home-telemetry room-telemetry stagger-item">
            <div className="home-tele-cell">
              <div className="home-tele-label">{t('room.energy')}</div>
              <div className="home-tele-value room-tele-text">{liveEnergy}</div>
              <div className="room-tele-bar">
                <div className="room-tele-bar-fill" style={{ width: liveEnergy }} />
              </div>
            </div>
            <div className="home-tele-cell">
              <div className="home-tele-label">{t('room.mood')}</div>
              <div className="home-tele-value room-tele-text" title={liveMood}>{liveMood}</div>
              {feed.length > 0 && (
                <div className="room-tele-sub" title={`${t('room.lastEvent')}: ${feed[feed.length - 1].text}`}>
                  {t('room.lastEvent')}: {feed[feed.length - 1].time} — {feed[feed.length - 1].text}
                </div>
              )}
            </div>
            <div className="home-tele-cell">
              <div className="home-tele-label">{t('room.pastimeNow')}</div>
              <div className="room-tele-row">
                <div className="home-tele-value room-tele-text" title={livePastime ?? pastime.label}>
                  {livePastime ?? pastime.label}
                </div>
                <span className="room-tele-actions">
                  <button
                    type="button"
                    className="room-tele-mini"
                    title={t('room.placePrev')}
                    onClick={() => setActivityIdx((i) => (i - 1 + spots.length) % spots.length)}
                  >
                    <Icon name="chevronLeft" size={12} />
                  </button>
                  <button
                    type="button"
                    className="room-tele-mini"
                    title={t('room.placeNext')}
                    onClick={() => setActivityIdx((i) => (i + 1) % spots.length)}
                  >
                    <Icon name="chevronRight" size={12} />
                  </button>
                </span>
              </div>
              {/* Прогресс цикла занятия (8с, как таймер сцены); key перезапускает
                  анимацию при смене занятия */}
              <div className="room-tele-bar">
                <div key={`${persona.id}-${activityIdx}`} className="room-tele-bar-fill room-tele-bar-fill--cycle" />
              </div>
            </div>
            <div className="home-tele-cell">
              <div className="home-tele-label">{t('room.placeInRoom')}</div>
              <div className="room-tele-row">
                <div className="home-tele-value room-tele-text" title={liveLocation ?? pastime.place}>{liveLocation ?? pastime.place}</div>
                <span className="room-tele-actions">
                  {spots.map((p, i) => (
                    <button
                      key={p.itemId != null ? `item-${p.itemId}` : p.key}
                      type="button"
                      className={`room-tele-dot ${i === activityIdx % spots.length ? 'room-tele-dot--active' : ''}`}
                      title={t('room.jumpTo', { place: p.place })}
                      aria-label={t('room.jumpTo', { place: p.place })}
                      onClick={() => setActivityIdx(i)}
                    />
                  ))}
                </span>
              </div>
              <div className="room-tele-sub">{pastime.duration}</div>
            </div>
          </div>
        </div>
      </div>
      )}

      {/* Арт-мастерская: конструктор аватара + промпт-паки и загрузка арта */}
      <div className="room-art">
        <div className="home-block-head">
          <span className="home-block-title">{t('room.artWorkshop')}</span>
          <span className="home-block-num">ART // IN-ROOM</span>
        </div>
        <div className="room-art-grid">
          {/* Промпт-пак и загрузка ассетов; конструктор аватара встроен в «Загрузку» */}
          <div className="room-art-side">
            <ArtPanel
              persona={persona}
              constructorSlot={
                <div className="room-builder-slot">
                  <div className="field-label">{t('room.avatarBuilder')}</div>
                  <div className="room-builder">
                    <div className="room-preview bracketed">
                      <div className="corner tl" />
                      <div className="corner br" />
                      <AvatarFigure config={draft} size={76} />
                    </div>
                    <div>
                      <div className="room-option-row">
                        <div className="field-label">{t('room.headShape')}</div>
                        <div className="room-options">
                          {headShapes.map((s) => (
                            <button
                              key={s.id}
                              type="button"
                              className={`room-option ${draft.head === s.id ? 'room-option--selected' : ''}`}
                              title={t(`room.head.${s.id}`)}
                              onClick={() => setDraft((d) => ({ ...d, head: s.id }))}
                            >
                              {s.glyph}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="room-option-row">
                        <div className="field-label">{t('room.eyes')}</div>
                        <div className="room-options">
                          {eyeOptions.map((e, i) => (
                            <button
                              key={e}
                              type="button"
                              className={`room-option ${draft.eyes === i ? 'room-option--selected' : ''}`}
                              onClick={() => setDraft((d) => ({ ...d, eyes: i }))}
                            >
                              {e}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="room-option-row">
                        <div className="field-label">{t('room.accessory')}</div>
                        <div className="room-options">
                          {accessoryOptions.map((a, i) => (
                            <button
                              key={a}
                              type="button"
                              className={`room-option ${draft.accessory === i ? 'room-option--selected' : ''}`}
                              onClick={() => setDraft((d) => ({ ...d, accessory: i }))}
                            >
                              {t(`room.acc.${a}`)}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div className="room-option-row">
                        <div className="field-label">{t('room.bodyShade')}</div>
                        <div className="room-options">
                          {avatarShades.map((s, i) => (
                            <button
                              key={s}
                              type="button"
                              className={`room-shade ${draft.shade === i ? 'room-shade--selected' : ''}`}
                              style={{ background: s }}
                              aria-label={t('room.shadeN', { n: i + 1 })}
                              onClick={() => setDraft((d) => ({ ...d, shade: i }))}
                            />
                          ))}
                        </div>
                      </div>
                      <button
                        className="btn btn--primary"
                        onClick={() => setAppliedByPersona((prev) => ({ ...prev, [persona.id]: draft }))}
                      >
                        {t('common.apply')}
                      </button>
                    </div>
                  </div>
                </div>
              }
            />
          </div>
        </div>
      </div>

      {/* Инвентарь и лента: при активном скине живут внутри скина */}
      {!skinActive && (
      <div className="two-col">
        {/* Инвентарь текущей персоны */}
        <div className="card stagger-item">
          <div className="card-title-row">
            <h3 className="card-title">{t('room.inventory')}</h3>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <button type="button" className="btn btn--ghost" onClick={() => setEditorOpen(true)}>
                <Icon name="pin" size={13} /> {t('editor.blockTitle')}
              </button>
              <span className="badge">{t('room.itemsBadge', { n: items.length })}</span>
            </span>
          </div>
          <div className="room-inventory-grid">
            {items.map((item) => (
              <div key={item.id} className={`room-item ${itemDraft?.id === item.id ? 'room-item--editing' : ''}`}>
                <div className="room-item-icon">
                  {item.image
                    ? <img className="room-item-img" src={item.image} alt={item.name} />
                    : <Icon name={itemIcon(item.icon)} size={20} />}
                  {item.marker && (
                    <span className="room-item-pin" title={t('room.markerTitle')}>
                      <Icon name="pin" size={10} />
                    </span>
                  )}
                </div>
                <div className="room-item-name">{item.name}</div>
                <div className="room-item-desc">{item.description}</div>
                <div className="room-item-foot">
                  <span className="badge">{t(`inv.tag.${item.tag}`)}</span>
                  <span className="room-item-actions">
                    <button type="button" className="btn btn--icon" title={t('room.editItem')} onClick={() => openEditItem(item)}>
                      <Icon name="pencil" size={12} />
                    </button>
                    <button type="button" className="btn btn--icon" title={t('room.deleteItem')} onClick={() => deleteItem(item.id)}>
                      <Icon name="trash" size={12} />
                    </button>
                  </span>
                </div>
              </div>
            ))}
          </div>

          {/* Редактор предмета: название, описание, вид (иконка/свой ассет),
              метка на фоне комнаты. Открыт — заменяет кнопку добавления */}
          {itemDraft ? (
            <div className="room-item-editor">
              <div className="field-label">{itemIsNew ? t('room.newItem') : t('room.editItem')}</div>
              <div className="room-item-editor-grid">
                <div className="field" style={{ marginBottom: 0 }}>
                  <label className="field-label" htmlFor="room-item-name">{t('room.itemName')}</label>
                  <input
                    id="room-item-name"
                    className="input"
                    placeholder={t('room.itemNamePh')}
                    value={itemDraft.name}
                    onChange={(e) => setItemDraft({ ...itemDraft, name: e.target.value })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label className="field-label" htmlFor="room-item-desc">{t('room.itemDesc')}</label>
                  <input
                    id="room-item-desc"
                    className="input"
                    value={itemDraft.description}
                    onChange={(e) => setItemDraft({ ...itemDraft, description: e.target.value })}
                  />
                </div>
              </div>

              <div className="field-label">{t('room.itemLook')}</div>
              <div className="room-options">
                {ICON_CHOICES.map((name) => (
                  <button
                    key={name}
                    type="button"
                    className={`room-option ${!itemDraft.image && itemDraft.icon === name ? 'room-option--selected' : ''}`}
                    onClick={() => setItemDraft({ ...itemDraft, icon: name, image: undefined })}
                  >
                    <Icon name={name} size={15} />
                  </button>
                ))}
                <button
                  type="button"
                  className={`room-option room-option--upload ${itemDraft.image ? 'room-option--selected' : ''}`}
                  title={t('room.uploadImage')}
                  onClick={() => itemImageInputRef.current?.click()}
                >
                  {itemDraft.image
                    ? <img className="room-item-img" src={itemDraft.image} alt={t('room.uploadImage')} />
                    : <><Icon name="photo" size={15} /> {t('room.uploadImage')}</>}
                </button>
                <input
                  ref={itemImageInputRef}
                  type="file"
                  accept="image/*"
                  hidden
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) pickItemImage(f);
                    e.target.value = '';
                  }}
                />
              </div>

              <div className="field-label">{t('room.markerTitle')}</div>
              <div className="room-item-editor-note">
                <button type="button" className="btn btn--ghost" onClick={() => setPlacerOpen(true)}>
                  <Icon name="pin" size={13} /> {t('room.placeBtn')}
                </button>
                {itemDraft.marker ? (
                  <>
                    <span className="ctx-note">{t('room.markerSet')}</span>
                    <button type="button" className="btn btn--ghost" onClick={() => setItemDraft({ ...itemDraft, marker: undefined })}>
                      {t('room.markerDel')}
                    </button>
                  </>
                ) : (
                  <span className="ctx-note">{t('room.markerNone')}</span>
                )}
              </div>

              {/* Место в комнате: предмет с меткой можно сделать точкой,
                  к которой персона подходит (попадает в цикл занятий) */}
              <div className="field-label">{t('room.placeInRoom')}</div>
              <div className="room-item-editor-note">
                <button
                  type="button"
                  className={`btn btn--ghost ${itemDraft.spot ? 'room-spot-toggle--on' : ''}`}
                  disabled={!itemDraft.marker}
                  onClick={() => setItemDraft({ ...itemDraft, spot: !itemDraft.spot })}
                >
                  <Icon name="personas" size={13} /> {itemDraft.spot ? t('room.spotOn') : t('room.spotOff')}
                </button>
                {!itemDraft.marker && <span className="ctx-note">{t('room.spotNeedMarker')}</span>}
              </div>

              <div className="room-item-editor-actions">
                <button type="button" className="btn btn--ghost" onClick={() => setItemDraft(null)}>
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn btn--primary" disabled={!itemDraft.name.trim()} onClick={saveItem}>
                  {itemIsNew ? t('common.add') : t('common.apply')}
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="btn btn--ghost room-add-btn" onClick={openNewItem}>
              + {t('room.addItem')}
            </button>
          )}
        </div>

        {/* Лента «пока тебя не было» текущей персоны */}
        <div className="card stagger-item">
          <div className="card-title-row">
            <h3 className="card-title">{t('room.feedTitle')}</h3>
            <span className="badge">LOG_{feed.length}</span>
          </div>
          <div className="room-feed">
            {feed.map((a) => (
              <div
                key={a.id}
                className={`room-feed-item${a.dim ? ' room-feed-item--dim' : ''}`}
                title={a.dim ? t('room.feedDim') : undefined}
              >
                <span className="room-feed-time">{a.time}</span>
                <span>{a.text}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      )}

      {/* Редактор размещения предметов — всплывающее окно (портал в <body>) */}
      {editorOpen &&
        createPortal(
          <div className="pcreate-overlay" onClick={() => setEditorOpen(false)}>
            <div className="pcreate-panel bracketed pcreate-panel--xl" onClick={(e) => e.stopPropagation()}>
              <div className="corner tl" />
              <div className="corner tr" />
              <div className="corner bl" />
              <div className="corner br" />
              <div className="pcreate-head">
                <span className="pcreate-title">{t('editor.blockTitle')}</span>
                <span className="badge">{persona.name}</span>
                <button
                  type="button"
                  className="pxe-close"
                  onClick={() => setEditorOpen(false)}
                  aria-label={t('common.close')}
                >
                  ✕
                </button>
              </div>
              <div className="pcreate-body">
                <RoomEditor persona={persona} />
              </div>
            </div>
          </div>,
          document.body,
        )}

      {/* Отдельный редактор размещения предмета на фоне комнаты */}
      {placerOpen && itemDraft && (
        <ItemPlacer
          name={itemDraft.name}
          icon={itemIcon(itemDraft.icon)}
          image={itemDraft.image}
          bg={roomBg?.dataUrl}
          initialMarker={itemDraft.marker}
          initialSize={itemDraft.size ?? 6}
          onCancel={() => setPlacerOpen(false)}
          onApply={(marker, size) => {
            setItemDraft({ ...itemDraft, marker: marker ?? undefined, size });
            setPlacerOpen(false);
          }}
        />
      )}
    </div>
  );
}

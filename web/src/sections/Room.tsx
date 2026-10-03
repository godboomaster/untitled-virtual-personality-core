import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import type { RoomAvatarPreset } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import ArtPanel from '../components/ArtPanel';
import RoomEditor from '../components/RoomEditor';
import SkinFrame from '../components/SkinFrame';
import Icon from '../components/icons';
import { usePersonaAvatars } from '../avatarStore';
import { usePersonaSkin } from '../skins/skinStore';
import { buildRoomPayload } from '../skins/payloads';
import { useShellTheme } from '../skins/shellTheme';
import AvatarBuilder from '../room/AvatarBuilder';
import InventoryCard from '../room/InventoryCard';
import RoomFeed from '../room/RoomFeed';
import { RoomFocusControls, RoomFocusOverlay } from '../room/RoomFocus';
import RoomScene from '../room/RoomScene';
import { claimPoke } from '../room/poke';
import RoomTelemetry, { AwayStrip } from '../room/RoomTelemetry';
import { patchRoomLayout } from '../room/roomLayoutStore';
import { spotPoint } from '../room/roomModel';
import { closeRoomPip, openRoomPip, roomPipSupported, useRoomPip } from '../room/roomPipStore';
import { setSessionAvatar, useRoomScene, useTitleStatus } from '../room/useRoomScene';

/* Раздел «Комната» — личное пространство персоны. Сцена показывает живое
   состояние персоны (GET /room раз в минуту): где она в комнате, чем занята
   и сколько уже, питомец, следы (новые вещи, записка с непрочитанным
   событием, ближайший план). Бэкенд недоступен — демо-режим: медленное
   расписание мест (смена раз в 3–5 минут), моки конфига и инвентаря.
   Сцена, аватар, телеметрия, инвентарь и лента — модули web/src/room/;
   модель сцены — useRoomScene (общая с PiP-окном, см. room/RoomPip.tsx),
   «поработать вместе» — room/focusStore.ts. */

// Подсветка события в ленте после клика по записке на столе
const HIGHLIGHT_MS = 4000;

export default function Room() {
  const { t } = useI18n();
  const { personas } = useMockData();
  // Текущая выбранная персона
  const [personaId, setPersonaId] = useState(() => personas[0].id);
  const persona = personas.find((p) => p.id === personaId) ?? personas[0];
  const sectionRef = useRef<HTMLDivElement>(null);

  // Вся модель сцены (живое состояние / демо, предметы, аватар, сигналы
  // присутствия, сессия фокуса) — общий хук с PiP-окном
  const room = useRoomScene(persona, { rootRef: sectionRef });
  const { view, mode, live, cfg, items, setItems, activity, activityKnown, duration, energy, energyPct, mood, feed, feedState, lastEvent, cues, focus } = room;
  const { pose } = room.sceneProps;
  const roomBg = room.art?.roomBg;
  const sprite = room.art?.sprite;
  const skinEnv = room.skinEnv;
  const applied = room.avatar;
  const markRead = room.markRead;

  // Заголовок вкладки, пока раздел открыт: «Имя · занятие» (+ счётчик непрочитанных)
  useTitleStatus('room', room.statusText);

  // Записка на столе: клик открывает ленту и подсвечивает событие
  const feedRef = useRef<HTMLDivElement>(null);
  const [highlightId, setHighlightId] = useState<number | null>(null);
  useEffect(() => {
    if (highlightId == null) return;
    const timer = setTimeout(() => setHighlightId(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [highlightId]);
  const openNote = useCallback(
    (id: number) => {
      markRead(id);
      setHighlightId(id);
      feedRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    },
    [markRead],
  );

  // Аватар: онлайн — в раскладку бэкенда, всегда — правка сессии (видна и в PiP)
  const applyAvatar = (next: RoomAvatarPreset) => {
    setSessionAvatar(persona.id, next);
    if (live) patchRoomLayout(persona.id, { avatar: next });
  };

  // PiP-окно: одно на приложение, привязано к персоне, для которой открыто
  const pip = useRoomPip();
  const pipHere = pip.win != null && pip.personaId === persona.id;
  const pipOk = roomPipSupported();

  // Оверлей сцены: полоса прогресса фокуса и пузырь реплики
  const overlay = useMemo(
    () => <RoomFocusOverlay focus={focus} personaId={persona.id} personaName={persona.name} />,
    [focus, persona.id, persona.name],
  );

  // Полноэкранный режим сцены: только комната поверх всего интерфейса
  const [sceneFullscreen, setSceneFullscreen] = useState(false);
  // Масштаб элементов сцены в фулскрине: px-размеры растут пропорционально
  // высоте экрана (базовая высота сцены в обычном режиме — 430px)
  // Масштаб под высоту модального окна (70vh), база — 430px высоты сцены;
  // ширина немасштабированного бокса — целевая ширина окна / k
  const measureFullscreen = (win: Window) => {
    const scale = (win.innerHeight * 0.7) / 430;
    return { scale, width: Math.min(win.innerWidth * 0.74, 1024) / scale };
  };
  const [fsBox, setFsBox] = useState<{ scale: number; width: number }>({ scale: 1, width: 1024 });
  const openFullscreen = () => {
    setFsBox(measureFullscreen(sectionRef.current?.ownerDocument.defaultView ?? window));
    setSceneFullscreen(true);
  };
  useEffect(() => {
    if (!sceneFullscreen) return;
    const win = sectionRef.current?.ownerDocument.defaultView ?? window;
    const update = () => setFsBox(measureFullscreen(win));
    win.addEventListener('resize', update);
    return () => win.removeEventListener('resize', update);
  }, [sceneFullscreen]);

  // Фулскрин — «одиночная страница»: блокируем прокрутку основного контента
  useEffect(() => {
    if (!sceneFullscreen) return;
    const body = sectionRef.current?.ownerDocument.body ?? document.body;
    const prev = body.style.overflow;
    body.style.overflow = 'hidden';
    return () => {
      body.style.overflow = prev;
    };
  }, [sceneFullscreen]);

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

  // Скин персоны: если файл комнаты задан и не сломан — сцена, телеметрия,
  // лента и инвентарь рендерятся внутри sandboxed iframe (арт-мастерская остаётся)
  const { skins, broken, reportBroken, reset } = usePersonaSkin(persona.id);
  const roomSkin = broken.room ? null : (skins.room ?? null);
  const skinActive = roomSkin != null;
  // Каркас приложения (сайдбар, топбар) красится в палитру скина персоны:
  // своего файла комнаты нет (или он сломан) — палитра любого другого
  // экрана того же скина, чтобы дефолтная комната не выпадала из темы
  useShellTheme(
    roomSkin
      ?? (broken.chat ? null : skins.chat)
      ?? (broken.dossier ? null : skins.dossier)
      ?? null,
  );
  const avatars = usePersonaAvatars();
  // Управление инвентарём при скине: карточка раскрывается кнопкой в шапке
  const [skinInventoryOpen, setSkinInventoryOpen] = useState(false);
  const avatarPoint = useMemo(
    () => spotPoint({ spotKey: activity.spot, config: cfg, floorPoints: roomBg?.floorPoints ?? null, items }),
    [activity.spot, cfg, roomBg, items],
  );
  const skinRoomState = skinActive
    ? buildRoomPayload({
        persona,
        statusText: t(`status.${persona.status}`),
        // Неизвестное занятие — прочерк, а не демо-расписание
        pastimeLabel: activityKnown ? activity.label : '—',
        pastimePlace: activityKnown ? activity.place : '',
        duration: duration ?? '',
        x: avatarPoint ? avatarPoint.x * 100 : 50,
        y: avatarPoint && roomBg ? avatarPoint.y * 100 : null,
        mood,
        energy,
        pet: cfg.pet,
        petLabel: cfg.petLabel,
        bg: roomBg?.dataUrl ?? null,
        sprite: sprite?.dataUrl ?? null,
        feed,
        inventory: items,
        env: skinEnv,
        spot: activity.spot,
        pose,
        pastimeSince: activity.since,
        avatar: avatars[persona.id],
      })
    : null;

  // Источник состояния — приглушённо в телеметрии
  const sourceLabel = live && view
    ? view.source.kind === 'telegram' ? t('room.source.telegram') : t('room.source.web')
    : mode === 'demo' ? t('room.source.offline') : null;

  const away = activityKnown && activity.spot === 'away';
  // Без бэкенда предметы не сохранить: правка инвентаря и размещения выключена
  const itemsOfflineTitle = mode === 'demo' ? t('room.feedEmpty.offline') : null;

  // Пустая лента — прочерк и причина
  const feedEmptyReason =
    feedState === 'offline' ? t('room.feedEmpty.offline')
      : feedState === 'loading' ? t('room.feedEmpty.loading')
        : feedState === 'life-off' ? t('room.feedEmpty.lifeOff')
          : feedState === 'empty' ? t('room.feedEmpty.empty')
            : null;

  const scene = (
    <RoomScene
      key={persona.id}
      {...room.sceneProps}
      editable
      overlay={overlay}
      onItemChange={room.updateItem}
      onNoteOpen={openNote}
      onClick={sceneFullscreen ? (e) => e.stopPropagation() : undefined}
      style={
        sceneFullscreen
          ? ({
              // Немасштабированный бокс = целевой размер окна / k;
              // после scale(k) от центра сцена занимает ровно 74vw × 70vh
              transform: `scale(${fsBox.scale})`,
              width: `${fsBox.width}px`,
              height: '430px',
            } as CSSProperties)
          : undefined
      }
    />
  );

  return (
    <div className="section" ref={sectionRef}>
      <div className="section-header">
        <div>
          <h2 className="section-title">{t('nav.room')}</h2>
          <p className="section-subtitle">
            {t('room.subtitle')}
          </p>
        </div>
        <div className="room-header-actions">
          {/* Поработать вместе: 25/50 мин, персона садится за работу */}
          <RoomFocusControls focus={focus} />
          {/* Скин рисует сцену и инвентарь сам — редактор размещения и
              управление предметами (правка, удаление) остаются здесь */}
          {skinActive && (
            <>
              <button
                type="button"
                className="btn btn--ghost"
                disabled={!!itemsOfflineTitle}
                title={itemsOfflineTitle ?? undefined}
                onClick={() => setEditorOpen(true)}
              >
                <Icon name="pin" size={13} /> {t('editor.blockTitle')}
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                aria-expanded={skinInventoryOpen}
                disabled={!!itemsOfflineTitle}
                title={itemsOfflineTitle ?? undefined}
                onClick={() => setSkinInventoryOpen((v) => !v)}
              >
                {t('room.inventory')} · {items.length}
              </button>
            </>
          )}
          {/* PiP: только Chromium (Document Picture-in-Picture); иначе кнопки нет */}
          {pipOk && (
            <button
              type="button"
              className="btn btn--ghost"
              title={pipHere ? t('room.pip.closeHint') : t('room.pip.openHint')}
              onClick={() => (pipHere ? closeRoomPip() : void openRoomPip(persona.id))}
            >
              {pipHere ? t('room.pip.back') : t('room.pip.open')}
            </button>
          )}
          <button
            type="button"
            className="btn btn--ghost"
            title={t('room.sceneFullscreen')}
            aria-label={t('room.sceneFullscreen')}
            onClick={openFullscreen}
          >
            <Icon name="fullscreen" size={13} />
          </button>
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

      {/* «Пока тебя не было»: что изменилось с прошлого визита */}
      {cues.away && <AwayStrip summary={cues.away} onDismiss={cues.dismissAway} />}

      {/* Скин персоны заменяет сцену и телеметрию целиком. Оверлей фокуса
          (полоса сессии, пузырь реплики) — поверх iframe, он вне контракта */}
      {skinActive && skinRoomState ? (() => {
        const frame = (fullscreen: boolean) => (
          <SkinFrame
            // В фулскрине iframe заполняет модальное окно (flex: 1 у --chat)
            className={fullscreen ? 'skin-frame skin-frame--chat' : 'skin-frame skin-frame--room'}
            skin={roomSkin}
            screen="room"
            state={skinRoomState}
            onAction={(action, values) => {
              // Действия из скина комнаты (whitelist). Предмет уходит в
              // инвентарь бэкенда; без бэкенда сохранить его негде — игнор
              if (action === 'add-inventory-item' && values.name?.trim() && !itemsOfflineTitle) {
                setItems([
                  ...items,
                  {
                    id: Date.now(),
                    icon: values.icon?.trim() || 'book',
                    name: values.name.trim(),
                    description: t('room.addedByOperator'),
                    tag: 'gift',
                  },
                ]);
              } else if (action === 'poke') {
                // Клик по аватару в скине — тот же тычок, что в сцене, с её кулдауном
                if (claimPoke(persona.id)) room.sceneProps.onPoke?.();
              }
            }}
            onError={(m) => reportBroken('room', m)}
            title={t('skin.frameTitle')}
          />
        );
        if (!sceneFullscreen) {
          return (
            <div style={{ position: 'relative' }}>
              {frame(false)}
              {overlay}
            </div>
          );
        }
        // Полный экран: скин в том же модальном окне, что и сцена (портал
        // в <body> — см. комментарий у сцены ниже)
        return createPortal(
          <div
            className="card room-scene-card room-scene-card--fullscreen"
            onClick={() => setSceneFullscreen(false)}
          >
            <div className="room-modal-frame">
              <div className="room-modal-head" onClick={(e) => e.stopPropagation()}>
                <div className="corner tl" />
                <div className="corner tr" />
                <span className="room-modal-title">
                  {persona.name} · {away ? t('room.statusAway') : (activityKnown && activity.label) || '—'}
                </span>
                <span className="badge">ROOM // LIVE</span>
                <button
                  type="button"
                  className="pxe-close"
                  title={t('room.sceneExitFullscreen')}
                  aria-label={t('room.sceneExitFullscreen')}
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
            <div
              className="room-scene"
              onClick={(e) => e.stopPropagation()}
              style={{ display: 'flex', flexDirection: 'column', width: 'min(74vw, 1024px)', height: '70vh' }}
            >
              {frame(true)}
              {overlay}
            </div>
          </div>,
          document.body,
        );
      })() : (
      <div className="room-layout">
        {/* Сцена комнаты. В фулскрине уходит порталом в <body>: анимации-предки
            (section-enter, stagger-item) держат transform и ломают position: fixed —
            без портала оверлей прокручивается вместе со страницей */}
        {(() => {
          const sceneCard = (
            <div
              className={`card room-scene-card stagger-item ${sceneFullscreen ? 'room-scene-card--fullscreen' : ''}`}
              onClick={sceneFullscreen ? () => setSceneFullscreen(false) : undefined}
            >
              {/* Модальный режим: рамка в стиле темы — шапка с заголовком и уголки.
                  По размеру совпадает с визуальным окном сцены (74vw/1024px × 70vh) */}
              {sceneFullscreen && (
                <div className="room-modal-frame">
                  <div className="room-modal-head" onClick={(e) => e.stopPropagation()}>
                    <div className="corner tl" />
                    <div className="corner tr" />
                    <span className="room-modal-title">
                      {persona.name} · {away ? t('room.statusAway') : (activityKnown && activity.label) || '—'}
                    </span>
                    <span className="badge">ROOM // LIVE</span>
                    <button
                      type="button"
                      className="pxe-close"
                      title={t('room.sceneExitFullscreen')}
                      aria-label={t('room.sceneExitFullscreen')}
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
              {scene}
            </div>
          );
          return sceneFullscreen ? createPortal(sceneCard, document.body) : sceneCard;
        })()}

        <div className="room-side">
          <RoomTelemetry
            energy={energy}
            energyPct={energyPct != null && Number.isFinite(energyPct) ? energyPct : null}
            mood={mood}
            lastEvent={lastEvent}
            pastime={away ? t('room.statusAway') : activityKnown ? activity.label || '—' : '—'}
            place={activityKnown ? activity.place || '—' : '—'}
            duration={duration}
            sourceLabel={sourceLabel}
          />
        </div>
      </div>
      )}

      {/* Инвентарь при скине: показ — в скине, управление (правка, удаление,
          новый предмет) — эта карточка по кнопке в шапке; лента — в скине */}
      {skinActive && skinInventoryOpen && (
        <InventoryCard
          key={persona.id}
          items={items}
          setItems={setItems}
          roomBgUrl={roomBg?.dataUrl}
          onOpenEditor={() => setEditorOpen(true)}
          offlineTitle={itemsOfflineTitle}
        />
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
                <AvatarBuilder key={`${persona.id}:${JSON.stringify(applied)}`} value={applied} seed={persona.id} onApply={applyAvatar} />
              }
            />
          </div>
        </div>
      </div>

      {/* Инвентарь и лента: при активном скине живут внутри скина */}
      {!skinActive && (
      <div className="two-col">
        <InventoryCard
          key={persona.id}
          items={items}
          setItems={setItems}
          roomBgUrl={roomBg?.dataUrl}
          onOpenEditor={() => setEditorOpen(true)}
          offlineTitle={itemsOfflineTitle}
        />
        <RoomFeed feed={feed} highlightId={highlightId} feedRef={feedRef} emptyReason={feedEmptyReason} />
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
    </div>
  );
}

/* Модель сцены комнаты — один путь вычислений для раздела «Комната» и
   PiP-окна: живое состояние (GET /room) или демо-расписание → место, поза,
   занятие, длительность; предметы, конфиг, арт, аватар; сигналы присутствия,
   записка на столе, сессия «поработать вместе». Возвращает готовые пропсы
   RoomScene (стабильные между рендерами, чтобы memo сцены держался) плюс
   данные для телеметрии, скина и заголовка вкладки.

   Документ-владелец (ownerDocument) управляет минутными часами, сигналами
   присутствия и паузой поллинга; PiP передаёт свой документ и keepAlive. */

import { createElement, Fragment, useCallback, useEffect, useMemo, useSyncExternalStore } from 'react';
import type { ReactNode, RefObject } from 'react';
import type { Persona, RoomActivity, RoomAvatarPreset } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import { usePersonaArt } from '../artStore';
import { setTitleStatus } from '../notifications';
import { useSkinEnv } from '../skins/useSkinEnv';
import type { SkinEnv } from '../skins/payloads';
import type { RoomSceneProps } from './RoomScene';
import { useFocusSession } from './focusStore';
import type { FocusApi } from './focusStore';
import { useMinuteNow } from './roomHooks';
import { useRoomLayout } from './roomLayoutStore';
import {
  defaultAvatarFor,
  demoStateAt,
  displayPose,
  durationSince,
  inferPose,
  inferSpot,
  resolveRoomConfig,
  withItemSpots,
} from './roomModel';
import type { SceneConfig } from './roomModel';
import type { RoomLiving, RoomLivingState, RoomPose, RoomView } from './roomTypes';
import { usePresenceCues, useUnreadNote } from './usePresenceCues';
import type { PresenceCues, PresenceSnapshot } from './usePresenceCues';
import { useRoomItems } from './useRoomItems';
import type { RoomItemsApi } from './useRoomItems';
import { useRoomView } from './useRoomView';
import type { RoomViewMode } from './useRoomView';

// ── Аватар, собранный в этой сессии (офлайн — поверх мока) ──
// Общий для раздела и PiP-окна: правка в конструкторе сразу видна в обоих

const sessionAvatars = new Map<string, RoomAvatarPreset>();
const avatarListeners = new Set<() => void>();

export function setSessionAvatar(persona: string, preset: RoomAvatarPreset) {
  sessionAvatars.set(persona, preset);
  avatarListeners.forEach((l) => l());
}

function subscribeAvatars(cb: () => void) {
  avatarListeners.add(cb);
  return () => {
    avatarListeners.delete(cb);
  };
}

// ── Статус в заголовке вкладки (владелец: 'room' | 'pip') ──

export function useTitleStatus(owner: string, text: string | null) {
  useEffect(() => {
    setTitleStatus(owner, text);
  }, [owner, text]);
  // Уход из раздела / закрытие PiP — заголовок восстанавливается
  useEffect(() => () => setTitleStatus(owner, null), [owner]);
}

export interface RoomActivityNow {
  spot: string;
  pose: RoomPose;
  label: string;
  place: string;
  since: number | null; // epoch, сек
}

export type RoomSceneBaseProps = Pick<
  RoomSceneProps,
  | 'personaId' | 'personaName' | 'config' | 'spotKey' | 'pose' | 'statusLine' | 'avatar' | 'sprite'
  | 'sprites' | 'roomBg' | 'items' | 'itemsReady' | 'timeOfDay' | 'weather' | 'note' | 'nextPlan' | 'onPoke'
>;

export interface RoomSceneModel extends RoomItemsApi {
  view: RoomView | null;
  mode: RoomViewMode;
  live: boolean;
  loaded: boolean;
  living: RoomLiving | null;
  state: RoomLivingState | null;
  cfg: SceneConfig;
  art: ReturnType<typeof usePersonaArt>;
  avatar: RoomAvatarPreset;
  activity: RoomActivityNow; // с учётом сессии фокуса
  duration: string | null;
  energy: string;
  energyPct: number;
  mood: string;
  feed: RoomActivity[];
  lastEvent: RoomActivity | null;
  markRead: (id: number) => void;
  cues: PresenceCues;
  skinEnv: SkinEnv;
  focus: FocusApi;
  statusText: string; // «Имя · занятие» — заголовок вкладки
  sceneProps: RoomSceneBaseProps;
}

export interface UseRoomSceneOptions {
  rootRef: RefObject<Element | null>;
  ownerDocument?: Document | null;
  keepAlive?: boolean;
  // Строка статуса с местом (PiP: имя · занятие · место · длительность)
  statusWithPlace?: boolean;
}

export function useRoomScene(persona: Persona, opts: UseRoomSceneOptions): RoomSceneModel {
  const { rootRef, ownerDocument = null, keepAlive = false, statusWithPlace = false } = opts;
  const { t, lang } = useI18n();
  const { activitiesByPersona, roomConfigs } = useMockData();

  // Живая комната с бэкенда: live — /room, legacy — старый /state, demo — офлайн
  const viewOpts = useMemo(() => ({ ownerDocument, keepAlive }), [ownerDocument, keepAlive]);
  const { view, mode, loaded } = useRoomView(persona.id, viewOpts);
  const live = mode === 'live';
  const living = view?.living && view.living.enabled && view.living.ui_sync && view.living.state ? view.living : null;
  const state = living?.state ?? null;

  // Пользовательский арт персоны (спрайт / фон комнаты / спрайты по позам)
  const art = usePersonaArt(persona.id);

  // Предметы: бэкенд (инвентарь + раскладка) или локальный стор поверх моков
  const itemsApi = useRoomItems(persona.id, viewOpts);
  const { items } = itemsApi;
  const layout = useRoomLayout(persona.id, live);

  // Конфиг сцены: бэкенд → мок этой персоны → общий дефолт
  const mockCfg = roomConfigs[persona.id];
  const baseCfg = useMemo(() => resolveRoomConfig({ view, live, mock: mockCfg, t }), [view, live, mockCfg, t]);
  const cfg = useMemo(() => withItemSpots(baseCfg, items, t), [baseCfg, items, t]);

  // Минутные часы документа-владельца: «уже N мин» и демо-расписание
  const now = useMinuteNow(rootRef, ownerDocument);

  // Текущее место/поза/занятие: живое состояние или демо-расписание
  const base = useMemo<RoomActivityNow>(() => {
    if (state) {
      const spot = inferSpot(state, cfg.spots);
      const spotCfg = cfg.spots.find((s) => s.key === spot);
      return {
        spot,
        pose: inferPose(state, spot, cfg.spots),
        label: state.pastime || spotCfg?.label || '',
        place: spot === 'away' ? state.location || t('room.statusAway') : spotCfg?.place || state.location || '',
        since: state.pastime_since ?? null,
      };
    }
    const d = demoStateAt(now, cfg.spots, persona.id);
    return { spot: d.spot, pose: d.pose, label: d.label, place: d.place, since: d.since };
  }, [state, cfg.spots, now, persona.id, t]);

  // Сессия «поработать вместе»: сверка с view.focus, таймер окончания
  const focus = useFocusSession(persona.id, { focus: view?.focus, mode, loaded, doc: ownerDocument });
  const focusStart = focus.session?.startedAt ?? null;

  // Во время сессии персона работает: за столом пишет, иначе читает на месте
  const activity = useMemo<RoomActivityNow>(() => {
    if (focusStart == null) return base;
    const desk = cfg.spots.find((s) => s.key === 'desk');
    const here = base.spot !== 'away' && base.spot !== 'bed' ? cfg.spots.find((s) => s.key === base.spot) : null;
    const other = cfg.spots.find((s) => s.key !== 'away' && s.key !== 'bed');
    const spot = desk ?? here ?? other;
    return {
      spot: spot?.key ?? 'desk',
      pose: desk ? 'write' : 'read',
      label: t('room.focus.working'),
      place: spot?.place ?? base.place,
      since: focusStart / 1000,
    };
  }, [base, focusStart, cfg.spots, t]);
  const duration = focus.active ? t('room.focus.left', { n: focus.remainingMin }) : durationSince(activity.since, now, t);

  const energyPct = state ? Math.max(0, Math.min(100, Math.round(state.energy))) : parseInt(cfg.energy ?? '', 10);
  const energy = state ? `${energyPct}%` : (cfg.energy ?? '—');
  const mood = state ? state.mood.tag : (cfg.mood ?? '—');

  // Лента «пока тебя не было»: недавние события офлайн-жизни (world/state)
  // независимо от consumed — после дневника/инициативы факты остаются
  // видимыми (приглушённо). Моки — только когда событий нет вовсе
  const feed = useMemo<RoomActivity[]>(() => {
    const events = living?.recent_events ?? [];
    if (!events.length) return state ? [] : (activitiesByPersona[persona.id] ?? []);
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
  }, [living, state, activitiesByPersona, persona.id]);
  const lastEvent = feed.length ? feed[feed.length - 1] : null;

  // Настоящие данные комнаты: онлайн и раскладка пришла (скрытые предметы
  // уже отфильтрованы). Демо-расписание и моки в снимок «пока тебя не было» и
  // в «увиденные предметы» не попадают — иначе после подключения бэкенда
  // сводка и подсветка сработали бы ложно на разнице моков и реальности
  const itemsReady = loaded && live && layout != null;

  // Сигналы присутствия: «с тобой», взгляд при возвращении, сводка отсутствия.
  // Снимок — по занятию без сессии фокуса («работаем вместе» — не событие)
  const snapshot = useMemo<PresenceSnapshot | null>(
    () =>
      itemsReady
        ? { pastime: base.label, place: base.place, items: items.map((i) => i.name), eventIds: feed.map((f) => f.id) }
        : null,
    [itemsReady, base.label, base.place, items, feed],
  );
  const cues = usePresenceCues({
    personaId: persona.id,
    source: live ? (view?.source ?? null) : null,
    snapshot,
    rootRef,
    ownerDocument,
  });
  // Во время сессии «с тобой» не отрывает от работы — остаётся только взгляд
  const pose = displayPose(activity.pose, focus.active && cues.cue === 'with_you' ? null : cues.cue);

  // Записка на столе: последнее непрочитанное событие
  const { note, markRead } = useUnreadNote(persona.id, feed);

  // Клик по аватару: сервер сам решает, дойдёт ли это до LLM
  const personaKey = persona.id;
  const onPoke = useCallback(() => {
    if (live) api.pokeRoom(personaKey).catch(() => {});
  }, [live, personaKey]);

  // Аватар: правка сессии → раскладка бэкенда → мок → дефолт
  const sessionAvatar = useSyncExternalStore(subscribeAvatars, () => sessionAvatars.get(persona.id));
  const avatar = useMemo(
    () => sessionAvatar ?? layout?.avatar ?? cfg.avatar ?? defaultAvatarFor(persona.id),
    [sessionAvatar, layout?.avatar, cfg.avatar, persona.id],
  );

  // Окружение: время суток и погода красят сцену (и уходят в скин)
  const skinEnv = useSkinEnv({ locale: lang, t, personaName: persona.name, apiOnline: mode !== 'demo', enabled: true });

  const away = activity.spot === 'away';
  const mainText = away ? t('room.statusAway') : activity.label;
  const placeText = statusWithPlace && !away && activity.place ? activity.place : '';
  const statusLine = useMemo<ReactNode>(
    () =>
      createElement(
        Fragment,
        null,
        `${persona.name} · `,
        createElement('span', { className: 'val' }, mainText),
        placeText ? ` · ${placeText}` : '',
        duration ? ` · ${duration}` : '',
      ),
    [persona.name, mainText, placeText, duration],
  );
  const statusText = focus.active
    ? `${persona.name} · ${mainText} · ${t('room.focus.left', { n: focus.remainingMin })}`
    : `${persona.name} · ${mainText}`;

  const nextPlan = living?.plans?.[0] ?? null;
  const weather = skinEnv.weather?.condition;
  const sceneProps = useMemo<RoomSceneBaseProps>(
    () => ({
      personaId: persona.id,
      personaName: persona.name,
      config: cfg,
      spotKey: activity.spot,
      pose,
      statusLine,
      avatar,
      sprite: art?.sprite,
      sprites: art?.sprites,
      roomBg: art?.roomBg,
      items,
      itemsReady,
      timeOfDay: skinEnv.timeOfDay,
      weather,
      note,
      nextPlan,
      onPoke,
    }),
    [persona.id, persona.name, cfg, activity.spot, pose, statusLine, avatar, art, items, itemsReady, skinEnv.timeOfDay, weather, note, nextPlan, onPoke],
  );

  return {
    ...itemsApi,
    view,
    mode,
    live,
    loaded,
    living,
    state,
    cfg,
    art,
    avatar,
    activity,
    duration,
    energy,
    energyPct,
    mood,
    feed,
    lastEvent,
    markRead,
    cues,
    skinEnv,
    focus,
    statusText,
    sceneProps,
  };
}

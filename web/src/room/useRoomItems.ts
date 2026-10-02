/* Предметы комнаты — один источник для сцены, карточки инвентаря и
   редактора размещения. Онлайн (бэкенд с /room): инвентарь из GET /room +
   раскладка layout.json + зоны размещения LLM; правки меток/размеров/иконок
   уходят в PUT /room/layout (дебаунс в roomLayoutStore), добавление и
   удаление — в инвентарь бэкенда. Офлайн/старый бэкенд — прежнее поведение:
   локальный inventoryStore поверх моков. */

import { useCallback, useMemo } from 'react';
import { api } from '../api';
import { alertDialog } from '../dialogStore';
import { useI18n, useMockData } from '../i18n';
import { setPersonaItems, usePersonaItems } from '../inventoryStore';
import type { InventoryItem } from '../mockData';
import { buildOnlineItems } from './roomModel';
import { patchRoomLayout, useRoomLayout } from './roomLayoutStore';
import type { RoomItemSpot, RoomLayoutItem, RoomPose } from './roomTypes';
import { refreshRoomView, useRoomView } from './useRoomView';
import type { UseRoomViewOptions } from './useRoomView';

const EMPTY_ITEMS: InventoryItem[] = [];

export interface RoomItemsApi {
  items: InventoryItem[];
  online: boolean;
  setItems: (next: InventoryItem[]) => void;
  updateItem: (id: number, patch: Partial<InventoryItem>) => void;
}

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

export function useRoomItems(persona: string, opts: UseRoomViewOptions = {}): RoomItemsApi {
  const { t } = useI18n();
  const { inventoryByPersona } = useMockData();
  const { view, online } = useRoomView(persona, opts);
  const layout = useRoomLayout(persona, online);
  const localOverride = usePersonaItems(persona);

  const items = useMemo(() => {
    if (online && view) {
      return buildOnlineItems({
        inventory: view.inventory,
        layout: layout?.items ?? {},
        placements: view.placements ?? {},
      });
    }
    return localOverride ?? inventoryByPersona[persona] ?? EMPTY_ITEMS;
  }, [online, view, layout, localOverride, inventoryByPersona, persona]);

  // Описание места вокруг предмета при включении тоггла «персона подходит»
  const spotFor = useCallback(
    (i: InventoryItem): RoomItemSpot =>
      i.spotInfo
        ? { label: i.spotInfo.label, place: i.spotInfo.place, pose: i.spotInfo.pose as RoomPose }
        : { label: t('room.itemPastime', { name: i.name }), place: i.name, pose: 'stand' },
    [t],
  );

  // Полный набор полей раскладки предмета (для новых и переименованных).
  // spot: null в раскладке — явный запрет места (главнее размещения LLM),
  // поэтому пишем его, только если место выключили явно; иначе LLM может
  // сама сделать место вокруг нового предмета (гитара, мольберт)
  const layoutOf = useCallback(
    (i: InventoryItem, spotOff = false): Partial<RoomLayoutItem> => ({
      marker: i.marker ?? null,
      ...(i.size != null ? { size: i.size } : {}),
      icon: i.icon,
      image: i.image ?? null,
      ...(i.spot ? { spot: spotFor(i) } : spotOff ? { spot: null } : {}),
    }),
    [spotFor],
  );

  // Разница двух версий предмета → патч раскладки (только изменённые поля)
  const diffLayout = useCallback(
    (cur: InventoryItem, next: InventoryItem): Partial<RoomLayoutItem> | null => {
      const out: Partial<RoomLayoutItem> = {};
      if (!sameJson(cur.marker, next.marker)) out.marker = next.marker ?? null;
      if ((cur.size ?? null) !== (next.size ?? null) && next.size != null) out.size = next.size;
      if (cur.icon !== next.icon) out.icon = next.icon;
      if ((cur.image ?? null) !== (next.image ?? null)) out.image = next.image ?? null;
      if (!!cur.spot !== !!next.spot) out.spot = next.spot ? spotFor(next) : null;
      return Object.keys(out).length ? out : null;
    },
    [spotFor],
  );

  const fail = (e: unknown) => void alertDialog({ message: e instanceof Error ? e.message : String(e) });

  const setItems = useCallback(
    (next: InventoryItem[]) => {
      if (!online) {
        setPersonaItems(persona, next);
        return;
      }
      const byId = new Map(items.map((i) => [i.id, i]));
      const nextIds = new Set(next.map((i) => i.id));
      const layoutPatch: Record<string, Partial<RoomLayoutItem> | null> = {};
      const jobs: (() => Promise<unknown>)[] = [];
      for (const cur of items) {
        if (nextIds.has(cur.id)) continue;
        layoutPatch[cur.name] = null;
        jobs.push(() => api.removeInventoryItem(persona, cur.name));
      }
      for (const n of next) {
        const cur = byId.get(n.id);
        if (!cur) {
          layoutPatch[n.name] = layoutOf(n);
          jobs.push(() => api.addInventoryItem(persona, n.name, n.description));
          continue;
        }
        if (cur.name !== n.name || cur.description !== n.description) {
          // Правки названия/описания у инвентаря бэкенда нет — удалить и добавить заново
          if (cur.name !== n.name) layoutPatch[cur.name] = null;
          layoutPatch[n.name] = layoutOf(n, (cur.spot && !n.spot) || layout?.items[cur.name]?.spot === null);
          jobs.push(async () => {
            await api.removeInventoryItem(persona, cur.name);
            await api.addInventoryItem(persona, n.name, n.description);
          });
          continue;
        }
        const d = diffLayout(cur, n);
        if (d) layoutPatch[n.name] = d;
      }
      if (Object.keys(layoutPatch).length) patchRoomLayout(persona, { items: layoutPatch });
      if (jobs.length) {
        void (async () => {
          try {
            for (const job of jobs) await job();
          } catch (e) {
            fail(e);
          }
          void refreshRoomView(persona);
        })();
      }
    },
    [online, persona, items, layout, layoutOf, diffLayout],
  );

  const updateItem = useCallback(
    (id: number, patch: Partial<InventoryItem>) => {
      setItems(items.map((i) => (i.id === id ? { ...i, ...patch } : i)));
    },
    [items, setItems],
  );

  return { items, online, setItems, updateItem };
}

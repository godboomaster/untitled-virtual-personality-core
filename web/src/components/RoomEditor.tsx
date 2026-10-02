import { useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { usePersonaArt } from '../artStore';
import { useRoomItems } from '../room/useRoomItems';
import Icon from './icons';
import { itemIcon } from './iconChoices';
import type { InventoryItem, Persona } from '../mockData';

/* Редактор размещения предметов (блок внутри раздела «Комната»):
   большой стейдж с фоном — предметы таскаются мышью, размер меняется
   уголком или слайдером; предмет без метки ставится кликом по фону.
   Данные — useRoomItems (синхронен со сценой и инвентарём). */

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

export default function RoomEditor({ persona }: { persona: Persona }) {
  const { t } = useI18n();
  const art = usePersonaArt(persona.id);
  const roomBg = art?.roomBg;

  // Инвентарь: онлайн — бэкенд (инвентарь + раскладка /room/layout),
  // иначе правки из общего стора поверх моков текущей локали
  const { items, setItems } = useRoomItems(persona.id);

  // Выбранный в списке предмет (его метка/размер правятся на стейдже)
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = items.find((i) => i.id === selectedId) ?? null;

  const stageRef = useRef<HTMLDivElement>(null);
  const dragItemId = useRef<number | null>(null);
  const resizeState = useRef<{ id: number; startX: number; startSize: number } | null>(null);

  const updateItem = (id: number, patch: Partial<InventoryItem>) =>
    setItems(items.map((i) => (i.id === id ? { ...i, ...patch } : i)));

  // Точка указателя в долях стейджа (0..1)
  const stageFraction = (e: React.PointerEvent) => {
    const stage = stageRef.current;
    if (!stage) return null;
    const r = stage.getBoundingClientRect();
    return {
      x: clamp01((e.clientX - r.left) / r.width),
      y: clamp01((e.clientY - r.top) / r.height),
      width: r.width,
    };
  };

  // Клик по пустому фону: предмет без метки встаёт в точку клика,
  // иначе клик просто снимает выбор
  const onStageDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (selected && !selected.marker) {
      const p = stageFraction(e);
      if (p) updateItem(selected.id, { marker: { x: p.x, y: p.y } });
    } else {
      setSelectedId(null);
    }
  };

  const onItemDown = (id: number) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragItemId.current = id;
    setSelectedId(id);
    const p = stageFraction(e);
    if (p) updateItem(id, { marker: { x: p.x, y: p.y } });
  };
  const onResizeDown = (item: InventoryItem) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    resizeState.current = { id: item.id, startX: e.clientX, startSize: item.size ?? 6 };
  };
  const onItemPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragItemId.current != null) {
      const p = stageFraction(e);
      if (p) updateItem(dragItemId.current, { marker: { x: p.x, y: p.y } });
    } else if (resizeState.current) {
      const p = stageFraction(e);
      if (!p) return;
      const { id, startX, startSize } = resizeState.current;
      const next = Math.max(2, Math.min(25, startSize + ((e.clientX - startX) / p.width) * 100));
      updateItem(id, { size: Math.round(next * 10) / 10 });
    }
  };
  const onItemPointerUp = () => {
    dragItemId.current = null;
    resizeState.current = null;
  };

  return (
    <div className="editor-layout">
        {/* Стейдж: фон комнаты + предметы с метками */}
        <div className="editor-stage bracketed" ref={stageRef} onPointerDown={onStageDown}>
          <div className="corner tl" />
          <div className="corner tr" />
          <div className="corner bl" />
          <div className="corner br" />
          {roomBg ? (
            <img className="room-marker-bg" src={roomBg.dataUrl} alt={t('editor.stageAlt')} draggable={false} />
          ) : (
            <div className="room-marker-nobg">{t('editor.noBg')}</div>
          )}
          {items
            .filter((i) => i.marker)
            .map((i) => (
              <div
                key={i.id}
                className={`room-item-marker ${selectedId === i.id ? 'room-item-marker--selected' : ''}`}
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
                  className={`room-item-resize ${selectedId === i.id ? 'room-item-resize--visible' : ''}`}
                  onPointerDown={onResizeDown(i)}
                  onPointerMove={onItemPointerMove}
                  onPointerUp={onItemPointerUp}
                />
              </div>
            ))}
        </div>

        {/* Список предметов: выбор, размер, снятие с фона */}
        <aside className="editor-side">
          <div className="field-label">{t('editor.items')}</div>
          <div className="editor-item-list">
            {items.map((i) => (
              <button
                key={i.id}
                type="button"
                className={`editor-item-row ${selectedId === i.id ? 'editor-item-row--active' : ''}`}
                onClick={() => setSelectedId(i.id)}
              >
                <span className="editor-item-icon">
                  {i.image
                    ? <img className="room-item-img" src={i.image} alt={i.name} />
                    : <Icon name={itemIcon(i.icon)} size={15} />}
                </span>
                <span className="editor-item-name">{i.name}</span>
                {i.marker && <Icon name="pin" size={11} />}
              </button>
            ))}
          </div>

          {selected ? (
            <div className="editor-controls">
              {!selected.marker && <div className="ctx-note">{t('editor.clickToPlace')}</div>}
              {selected.marker && (
                <>
                  <div className="place-row">
                    <span className="field-label">{t('room.markerSize')}</span>
                    <input
                      type="range"
                      min={2}
                      max={25}
                      step={0.5}
                      value={selected.size ?? 6}
                      onChange={(e) => updateItem(selected.id, { size: Number(e.target.value) })}
                      style={{ flex: 1 }}
                    />
                  </div>
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => updateItem(selected.id, { marker: undefined })}
                  >
                    {t('room.placeRemove')}
                  </button>
                </>
              )}
            </div>
          ) : (
            <div className="ctx-note">{t('editor.selectItem')}</div>
          )}
        </aside>
    </div>
  );
}

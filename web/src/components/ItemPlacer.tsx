import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import Icon from './icons';
import type { IconName } from './icons';
import { useI18n } from '../i18n';

/* Отдельный редактор размещения предмета на фоне комнаты: большое превью
   фона, предмет таскается мышью в нужное место, размер меняется уголком
   или слайдером. Рендерится порталом в <body> (transform у анимаций-предков
   ломает position: fixed). */

interface MarkerPoint {
  x: number;
  y: number;
}

interface ItemPlacerProps {
  name: string;
  icon: IconName;
  image?: string; // dataURL собственного ассета предмета
  bg?: string; // dataURL фона комнаты
  initialMarker?: MarkerPoint;
  initialSize: number; // % ширины фона
  onApply: (marker: MarkerPoint | null, size: number) => void;
  onCancel: () => void;
}

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

export default function ItemPlacer({
  name,
  icon,
  image,
  bg,
  initialMarker,
  initialSize,
  onApply,
  onCancel,
}: ItemPlacerProps) {
  const { t } = useI18n();
  const stageRef = useRef<HTMLDivElement>(null);
  // Метка: существующая или по центру — предмет сразу видно на превью
  const [marker, setMarker] = useState<MarkerPoint>(initialMarker ?? { x: 0.5, y: 0.75 });
  const [size, setSize] = useState(initialSize);
  const dragMode = useRef<'move' | 'resize' | null>(null);
  const resizeStart = useRef({ x: 0, size: 0 });

  // Закрытие по Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  // Перенос метки за указателем (координаты в долях кадра 0..1)
  const moveTo = (e: React.PointerEvent) => {
    const stage = stageRef.current;
    if (!stage) return;
    const r = stage.getBoundingClientRect();
    setMarker({
      x: clamp01((e.clientX - r.left) / r.width),
      y: clamp01((e.clientY - r.top) / r.height),
    });
  };

  const onItemDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    dragMode.current = 'move';
    moveTo(e);
  };
  const onHandleDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation(); // уголок не должен начинать перетаскивание
    e.currentTarget.setPointerCapture(e.pointerId);
    dragMode.current = 'resize';
    resizeStart.current = { x: e.clientX, size };
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragMode.current === 'move') {
      moveTo(e);
    } else if (dragMode.current === 'resize') {
      const r = stageRef.current?.getBoundingClientRect();
      if (!r) return;
      const next = resizeStart.current.size + ((e.clientX - resizeStart.current.x) / r.width) * 100;
      setSize(Math.round(Math.max(2, Math.min(25, next)) * 10) / 10);
    }
  };
  const onPointerUp = () => {
    dragMode.current = null;
  };

  const modal = (
    <div className="pcreate-overlay" onClick={onCancel}>
      <div className="pcreate-panel bracketed pcreate-panel--xl" onClick={(e) => e.stopPropagation()}>
        <div className="corner tl" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br" />
        <div className="pcreate-head">
          <span className="pcreate-title">{t('room.placeTitle')}</span>
          <span className="badge">{name || t('room.newItem')}</span>
          <button type="button" className="pxe-close" onClick={onCancel} aria-label={t('common.close')}>
            ✕
          </button>
        </div>
        <div className="pcreate-body">
          {/* Колонка-обёртка: у xl-модалок body — flex-строка (правило
              для YAML-редактора), без неё стейдж схлопывается в ноль */}
          <div className="place-body">
            <div className="place-stage" ref={stageRef}>
            {bg ? (
              <img className="room-marker-bg" src={bg} alt={t('room.placeTitle')} draggable={false} />
            ) : (
              <div className="room-marker-nobg">{t('room.markerNoBg')}</div>
            )}
            <div
              className="room-item-marker"
              style={{ left: `${marker.x * 100}%`, top: `${marker.y * 100}%`, width: `${size}%` }}
              title={name}
              onPointerDown={onItemDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
            >
              {image
                ? <img className="room-item-marker-img" src={image} alt={name} draggable={false} />
                : <Icon name={icon} size={22} />}
              <div
                className="room-item-resize room-item-resize--visible"
                onPointerDown={onHandleDown}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
              />
            </div>
          </div>
          <div className="place-row">
            <span className="field-label">{t('room.markerSize')}</span>
            <input
              type="range"
              min={2}
              max={25}
              step={0.5}
              value={size}
              onChange={(e) => setSize(Number(e.target.value))}
              style={{ flex: 1 }}
            />
          </div>
          <div className="field-hint">{t('room.placeHint')}</div>
          </div>
        </div>
        <div className="pcreate-foot">
          <button type="button" className="btn btn--danger" onClick={() => onApply(null, size)}>
            {t('room.placeRemove')}
          </button>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button type="button" className="btn btn--ghost" onClick={onCancel}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn btn--primary" onClick={() => onApply(marker, size)}>
              {t('common.apply')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}

import { useEffect, useRef, useState } from 'react';
import type { AnchorPoint, RoomBgAsset, SpriteAsset } from '../artStore';
import { useI18n } from '../i18n';

/* Калибровка загруженного арта: модалка с изображением и перетаскиваемыми
   метками. Для спрайта персоны — одна метка «ступни» (центр/ноги — точка
   контакта с полом), для фона комнаты — три точки пола (стол/центр/окно),
   между которыми перемещается персона. */

type AssetType = 'sprite' | 'roomBg';

interface ArtCalibratorProps {
  assetType: AssetType;
  sourceDataUrl: string;
  initialAnchor?: AnchorPoint;
  initialFloorPoints?: RoomBgAsset['floorPoints'];
  onApply: (result: SpriteAsset | RoomBgAsset) => void;
  onCancel: () => void;
}

const FLOOR_KEYS = ['desk', 'shelf', 'window'] as const;
type FloorKey = (typeof FLOOR_KEYS)[number];

const FLOOR_LABEL_KEYS: Record<FloorKey, string> = {
  desk: 'art.calFloorDesk',
  shelf: 'art.calFloorShelf',
  window: 'art.calFloorWindow',
};

const DEFAULT_FLOOR_POINTS: RoomBgAsset['floorPoints'] = {
  desk: { x: 0.15, y: 0.8 },
  shelf: { x: 0.5, y: 0.8 },
  window: { x: 0.85, y: 0.8 },
};

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

export default function ArtCalibrator({
  assetType,
  sourceDataUrl,
  initialAnchor,
  initialFloorPoints,
  onApply,
  onCancel,
}: ArtCalibratorProps) {
  const { t } = useI18n();
  const stageRef = useRef<HTMLDivElement>(null);
  // Ключ метки, которую сейчас тянут ('anchor' или точка пола)
  const dragKey = useRef<'anchor' | FloorKey | null>(null);
  const [anchor, setAnchor] = useState<AnchorPoint>(initialAnchor ?? { x: 0.5, y: 0.98 });
  const [floorPoints, setFloorPoints] = useState<RoomBgAsset['floorPoints']>(
    initialFloorPoints ?? DEFAULT_FLOOR_POINTS,
  );

  // Закрытие по Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  // Перетаскивание метки: координаты хранятся в долях кадра (0..1)
  const onMarkerDown = (key: 'anchor' | FloorKey) => (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragKey.current = key;
  };
  const onMarkerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const key = dragKey.current;
    const stage = stageRef.current;
    if (!key || !stage) return;
    const r = stage.getBoundingClientRect();
    const x = clamp01((e.clientX - r.left) / r.width);
    const y = clamp01((e.clientY - r.top) / r.height);
    if (key === 'anchor') setAnchor({ x, y });
    else setFloorPoints((prev) => ({ ...prev, [key]: { x, y } }));
  };
  const onMarkerUp = () => {
    dragKey.current = null;
  };

  const marker = (key: 'anchor' | FloorKey, p: AnchorPoint, label: string) => (
    <div
      key={key}
      className="cal-marker"
      style={{ left: `${p.x * 100}%`, top: `${p.y * 100}%` }}
      onPointerDown={onMarkerDown(key)}
      onPointerMove={onMarkerMove}
      onPointerUp={onMarkerUp}
    >
      <span>{label}</span>
    </div>
  );

  const apply = () => {
    if (assetType === 'sprite') onApply({ dataUrl: sourceDataUrl, anchor });
    else onApply({ dataUrl: sourceDataUrl, floorPoints });
  };

  return (
    <div className="pcreate-overlay" onClick={onCancel}>
      <div className="pcreate-panel bracketed pcreate-panel--wide" onClick={(e) => e.stopPropagation()}>
        <div className="corner tl" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br" />
        <div className="pcreate-head">
          <span className="pcreate-title">{t('art.calTitle')}</span>
          <span className="badge">{assetType === 'sprite' ? t('art.sprite') : t('art.roomBg')}</span>
          <button type="button" className="pxe-close" onClick={onCancel} aria-label={t('common.close')}>
            ✕
          </button>
        </div>
        <div className="pcreate-body">
          <div className="cal-stage" ref={stageRef}>
            <img className="cal-img" src={sourceDataUrl} alt={t('art.calTitle')} draggable={false} />
            {assetType === 'sprite'
              ? marker('anchor', anchor, t('art.calFeet'))
              : FLOOR_KEYS.map((k) => marker(k, floorPoints[k], t(FLOOR_LABEL_KEYS[k])))}
          </div>
          <div className="field-hint" style={{ marginTop: 10 }}>
            {t('art.calHint')}
          </div>
        </div>
        <div className="pcreate-foot">
          <button type="button" className="btn btn--ghost" onClick={onCancel}>
            {t('common.cancel')}
          </button>
          <button type="button" className="btn btn--primary" onClick={apply}>
            {t('common.apply')}
          </button>
        </div>
      </div>
    </div>
  );
}

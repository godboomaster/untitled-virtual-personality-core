import type { CSSProperties } from 'react';
import { useI18n } from '../i18n';
import AnimSprite from './AnimSprite';
import { animBox, animSink, type PortraitAnim } from './animTypes';
import { hasPortrait, usePortrait } from './portraitStore';
import { splitAvatar } from '../api';

/* Размер анимации на рабочем столе рядом с головой: длинная сторона кадра —
   сторона головы × scale (animBox). Голова — средняя клетка листа A (взгляд
   прямо), её нет — пустой квадрат. Обе стоят на одном полу, рамки — границы
   кадра. Не влезают в сцену — уменьшаются вместе: соотношение то же. */

const HEAD = 96; // сторона головы на сцене при ×1, px
const STAGE_W = 280;
const STAGE_H = 210;
const PAD = 14;
const GAP = 18;
const SCALE_MIN = 0.3;
const SCALE_MAX = 3;
// Ползунок — по логарифму: ×1 посередине, ×0.5 и ×2 — на равном расстоянии
const LOG_SPAN = Math.log(SCALE_MAX / SCALE_MIN);
const toPos = (v: number) => Math.log(v / SCALE_MIN) / LOG_SPAN;
const fromPos = (p: number) => Math.round(SCALE_MIN * Math.exp(p * LOG_SPAN) * 20) / 20;

export default function AnimSize({
  avatar,
  anim,
  scale,
  onChange,
}: {
  /** Чей аватар: id персоны или «персона@режим» (у режима без своей головы — обычная) */
  avatar: string;
  /** Сохранённая анимация; нет (новая, лист ещё не сохранён) — только голова */
  anim: PortraitAnim | null;
  scale: number;
  onChange: (v: number) => void;
}) {
  const { t } = useI18n();
  const own = usePortrait(avatar);
  const base = usePortrait(splitAvatar(avatar).persona);
  const portrait = hasPortrait(own) ? own : base;
  const shown = anim ? { ...anim, scale } : null;
  const box = shown ? animBox(shown, HEAD) : { w: 0, h: 0 };
  const fit = Math.min(1, (STAGE_H - 2 * PAD - 18) / Math.max(HEAD, box.h), (STAGE_W - 2 * PAD - GAP) / (HEAD + box.w));
  const head = Math.round(HEAD * fit);
  // Фигура в кадрах уходит под пол — голова на полу кадра, а не на его низе
  const sinkPx = shown ? Math.round(animSink(shown, head)) : 0;

  let headStyle: CSSProperties = { width: head, height: head, marginBottom: sinkPx };
  if (hasPortrait(portrait)) {
    const { rows, cols } = portrait;
    const r = Math.floor(rows / 2);
    const c = Math.floor(cols / 2);
    headStyle = {
      ...headStyle,
      backgroundImage: `url(${portrait.sheets.A})`,
      backgroundSize: `${cols * 100}% ${rows * 100}%`,
      backgroundPosition: `${cols > 1 ? (c / (cols - 1)) * 100 : 0}% ${rows > 1 ? (r / (rows - 1)) * 100 : 0}%`,
    };
  }
  const value = scale.toFixed(2).replace(/\.?0+$/, '');

  return (
    <div className="anim-size">
      <div className="anim-size-stage" style={{ width: STAGE_W, height: STAGE_H, padding: PAD, gap: GAP }}>
        <figure className="anim-size-item">
          <div className="anim-size-box anim-size-head" style={headStyle} />
          <figcaption>{t('anim.size.head')}</figcaption>
        </figure>
        <figure className="anim-size-item">
          {shown ? (
            <AnimSprite anim={shown} size={head} loop className="anim-size-box" />
          ) : (
            <div className="anim-size-box anim-size-none" style={{ width: head, height: head }}>
              {t('anim.size.none')}
            </div>
          )}
          <figcaption>{t('anim.size.anim', { v: value })}</figcaption>
        </figure>
      </div>
      <div className="anim-size-row">
        <input
          type="range"
          min={0}
          max={1}
          step={0.005}
          value={toPos(scale)}
          aria-label={t('anim.scale')}
          aria-valuetext={`×${value}`}
          onChange={(e) => onChange(Math.min(SCALE_MAX, Math.max(SCALE_MIN, fromPos(Number(e.target.value)))))}
        />
        <span className="anim-size-value">×{value}</span>
        <button type="button" className="btn btn--ghost" disabled={scale === 1} onClick={() => onChange(1)}>
          {t('anim.size.reset')}
        </button>
      </div>
      <div className="field-hint">{t('anim.size.hint')}</div>
    </div>
  );
}

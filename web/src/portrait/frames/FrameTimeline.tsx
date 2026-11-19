import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent } from 'react';
import { useI18n } from '../../i18n';
import { frameBox } from './frameAnalysis';
import { FrameThumb, NumField } from './frameFields';
import { cutOf } from './frameMath';
import { frameLimits, frameMs, frameStarts, stepOf, withFrameMs, withFrameStart } from './frameTiming';
import type { FrameTiming } from './frameTypes';
import type { FrameSource } from './useFrameEditor';

/* Полоска времени анимации: кадр — блок, его ширина — сколько он идёт. Блок
   тянут, чтобы переставить кадр на другое место (черта — куда встанет), с
   Alt (⌥) — чтобы кадр начался раньше или позже (следующий стоит на месте);
   правый край — чтобы кадр шёл дольше (следующие сдвигаются). «×» на блоке
   или Delete — убрать кадр из анимации. Пока тянут, масштаб полоски не
   меняется: блок не уезжает из-под мыши. Правки живут в редакторе вместе с
   остальными, до «Сохранить».

   Масштаб полоски (только вид, время кадров не меняет): − и + у полоски,
   Ctrl или ⌘ с колесом (щипок на тачпаде) или тянуть линейку со временем —
   полоска растягивается от точки под мышью. Мельче, чем «все кадры
   видны», не бывает; «Целиком» — как было сначала. */

const MIN_BLOCK = 30; // px на кадр в среднем: мельче не ухватить, полоска прокручивается
const THUMB_H = 44;
const SNAP_MS = 10;
const CLICK_PX = 3;
const TICK_MIN_PX = 56;
const CUT_WIDE = 64; // px блока: «лист 2» рядом с мс; уже — «л2» над мс, совсем узкий — без подписи
const CUT_NARROW = 26;
const TICKS = [10, 20, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000];
const ZOOM_MAX = 20; // во столько раз шире, чем вписано
const ZOOM_STEP = 1.5;
const RULER_PX = 120; // протянули линейку на столько — масштаб в e раз

interface Drag {
  edge: boolean;
  shift: boolean; // Alt: сдвиг по времени, а не перестановка
  i: number;
  x: number;
  px: number;
  base: FrameTiming;
  tag: string;
}

export default function FrameTimeline({
  source,
  timing,
  cell,
  always,
  playing,
  onPlay,
  onPick,
  onChange,
  onMove,
  onDelete,
}: {
  source: FrameSource;
  timing: FrameTiming;
  cell: number | null; // какой кадр открыт в выбранном слое; другой лист — null
  always: boolean; // все поводы — циклы: и так по кругу
  playing: boolean;
  onPlay: () => void;
  onPick: (i: number) => void;
  onChange: (timing: FrameTiming, tag?: string) => void;
  onMove: (from: number, to: number) => void; // кадр from — перед кадром to
  onDelete: ((i: number) => void) | null; // null — кадр последний, убирать нечего
}) {
  const { t, lang } = useI18n();
  const boxRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [frozen, setFrozen] = useState<number | null>(null); // px на мс, пока тянут
  const drag = useRef<Drag | null>(null);
  const dragged = useRef(false);
  const trackRef = useRef<HTMLDivElement>(null);
  // Кадр переставляют: какой и перед каким встанет
  const [moving, setMoving] = useState<{ from: number; to: number } | null>(null);
  const movingRef = useRef(moving);
  const setMove = (m: { from: number; to: number } | null) => {
    movingRef.current = m;
    setMoving(m);
  };
  const [zoom, setZoom] = useState(1); // к вписанному масштабу
  // Какой момент держать под точкой ax полоски, когда масштаб сменится
  const keep = useRef<{ at: number; ax: number } | null>(null);
  const stretch = useRef<{ x: number; zoom: number; at: number; ax: number } | null>(null);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const n = source.count;
  const starts = frameStarts(timing, n);
  const total = starts[n];
  const fitW = Math.max(width, n * MIN_BLOCK); // ширина полоски «вписать»
  const zoomMin = fitW ? Math.min(1, width / fitW) : 1; // мельче — все кадры уже видны
  const z = Math.max(zoomMin, zoom); // полоска стала шире — сжатый масштаб упирается
  const px = frozen ?? (fitW / total) * z;
  const box = frameBox(source.data, source.fix, source.frameMax);
  const looped = always || timing.loop;
  const open = cell != null && cell < n ? cell : null;
  const sec = (ms: number) => (ms / 1000).toLocaleString(lang, { maximumFractionDigits: 2 });

  const tick = TICKS.find((v) => v * px >= TICK_MIN_PX) ?? TICKS[TICKS.length - 1];
  const ticks = Array.from({ length: Math.floor(total / tick) + 1 }, (_, i) => i * tick);

  // Масштаб z, момент под точкой ax полоски (px от её левого края) стоит на месте
  const zoomTo = (target: number, ax?: number) => {
    const el = boxRef.current;
    if (!el || !px) return;
    const next = Math.max(zoomMin, Math.min(ZOOM_MAX, target));
    if (next === z) return;
    let a = ax ?? el.clientWidth / 2;
    if (ax == null && open != null) {
      // Кнопками — вокруг открытого кадра, если его видно
      const mid = ((starts[open] + starts[open + 1]) / 2) * px - el.scrollLeft;
      if (mid >= 0 && mid <= el.clientWidth) a = mid;
    }
    keep.current = { at: (el.scrollLeft + a) / px, ax: a };
    setZoom(next);
  };
  const zoomRef = useRef(zoomTo);
  zoomRef.current = zoomTo;
  const zoomNow = useRef(zoom);
  zoomNow.current = z;

  useLayoutEffect(() => {
    const el = boxRef.current;
    const k = keep.current;
    if (!el || !k) return;
    keep.current = null;
    el.scrollLeft = k.at * px - k.ax;
  }, [px]);

  // Ctrl или ⌘ с колесом и щипок тачпада — масштаб у курсора (иначе — масштаб страницы)
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const d = Math.max(-60, Math.min(60, e.deltaY));
      zoomRef.current(zoomNow.current * Math.exp(-d * 0.01), e.clientX - el.getBoundingClientRect().left);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  // Линейку тянут — полоска растягивается от точки, где взяли
  const rulerDown = (e: PointerEvent<HTMLElement>) => {
    const el = boxRef.current;
    if (e.button !== 0 || !el || !px) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const ax = e.clientX - el.getBoundingClientRect().left;
    stretch.current = { x: e.clientX, zoom: z, at: (el.scrollLeft + ax) / px, ax };
  };
  const rulerMove = (e: PointerEvent<HTMLElement>) => {
    const st = stretch.current;
    if (!st) return;
    const next = Math.max(zoomMin, Math.min(ZOOM_MAX, st.zoom * Math.exp((e.clientX - st.x) / RULER_PX)));
    if (next === z) return;
    keep.current = { at: st.at, ax: st.ax };
    setZoom(next);
  };
  const rulerUp = () => {
    stretch.current = null;
  };

  const grab = (e: PointerEvent<HTMLElement>, edge: boolean, i: number) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragged.current = false;
    drag.current = { edge, shift: !edge && e.altKey, i, x: e.clientX, px, base: timing, tag: `timing:${Date.now()}` };
    setFrozen(px);
  };

  const pull = (e: PointerEvent<HTMLElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!dragged.current && Math.abs(dx) < CLICK_PX) return;
    dragged.current = true;
    if (!d.edge && !d.shift) {
      // Перестановка: встанет перед кадром, чья середина правее мыши
      const track = trackRef.current;
      const el = boxRef.current;
      if (!track || !el) return;
      const x = e.clientX - track.getBoundingClientRect().left;
      let to = n;
      for (let i = 0; i < n; i++) {
        if (((starts[i] + starts[i + 1]) / 2) * d.px > x) {
          to = i;
          break;
        }
      }
      setMove({ from: d.i, to });
      // У края полоски — прокрутить её
      const r = el.getBoundingClientRect();
      if (e.clientX < r.left + 24) el.scrollLeft -= 12;
      else if (e.clientX > r.right - 24) el.scrollLeft += 12;
      return;
    }
    // Shift — шагами скорости: ритм останется ровным
    const snap = e.shiftKey ? stepOf(d.base.fps) : SNAP_MS;
    const near = (ms: number) => Math.round(ms / snap) * snap;
    const shift = dx / d.px;
    const next = d.edge
      ? withFrameMs(d.base, n, d.i, near(frameMs(d.base, d.i) + shift))
      : withFrameStart(d.base, n, d.i, near(frameStarts(d.base, n)[d.i] + shift));
    onChange(next, d.tag);
  };

  const drop = () => {
    const m = movingRef.current;
    drag.current = null;
    setFrozen(null);
    setMove(null);
    if (m && m.to !== m.from && m.to !== m.from + 1) onMove(m.from, m.to);
  };

  const cancel = () => {
    drag.current = null;
    setFrozen(null);
    setMove(null);
  };

  const dropAt = moving && moving.to !== moving.from && moving.to !== moving.from + 1 ? moving.to : null;

  const { min, max } = frameLimits(timing.fps);

  return (
    <section className="fe-tl" aria-label={t('frames.tl.title')} style={{ '--fe-atlas': `url(${source.atlas})` } as CSSProperties}>
      <div className="fe-tl-bar">
        <button type="button" className={`fe-tool ${playing ? 'fe-tool--on' : ''}`} aria-pressed={playing} onClick={onPlay}>
          {t(playing ? 'frames.stop' : 'frames.play')}
        </button>
        <button
          type="button"
          className={`fe-tool ${looped ? 'fe-tool--on' : ''}`}
          aria-pressed={looped}
          disabled={always}
          title={t(always ? 'frames.tl.loopAlways' : 'frames.tl.loopHint')}
          onClick={() => onChange({ ...timing, loop: !timing.loop })}
        >
          {t('frames.tl.loop')}
        </button>
        <NumField
          label={t('frames.tl.speed')}
          value={timing.fps}
          min={1}
          max={30}
          suffix={t('frames.tl.fps')}
          onChange={(v) => onChange({ ...timing, fps: Math.round(v) }, 'timing:fps')}
        />
        {!looped && (
          <NumField
            label={t('frames.tl.repeats')}
            value={timing.loops}
            min={1}
            max={20}
            onChange={(v) => onChange({ ...timing, loops: Math.round(v) }, 'timing:loops')}
          />
        )}
        {open != null && (
          <NumField
            key={open}
            label={t('frames.frameTitle', { n: open + 1 })}
            value={Math.round(frameMs(timing, open))}
            step={SNAP_MS}
            min={Math.ceil(min)}
            max={Math.floor(max)}
            suffix={t('frames.tl.ms')}
            onChange={(v) => onChange(withFrameMs(timing, n, open, v), 'timing:ms')}
          />
        )}
        <button
          type="button"
          className="fe-tool fe-tool--del"
          disabled={open == null || !onDelete}
          title={t(open == null ? 'frames.deleteFramePick' : 'frames.deleteFrameHint')}
          onClick={() => open != null && onDelete?.(open)}
        >
          {open != null ? t('frames.deleteFrame', { n: open + 1 }) : t('frames.deleteFrameNone')}
        </button>
        <button type="button" className="fe-tool" disabled={!timing.holds} title={t('frames.tl.evenHint')} onClick={() => onChange({ ...timing, holds: null })}>
          {t('frames.tl.even')}
        </button>
        <span className="fe-tl-total">{t('frames.tl.total', { s: sec(total) })}</span>
        <div className="fe-tl-zoom" role="group" aria-label={t('frames.tl.zoom')}>
          <button type="button" className="fe-zoom-btn" aria-label={t('frames.tl.zoomOut')} title={t('frames.tl.zoomOut')} disabled={z <= zoomMin + 1e-6} onClick={() => zoomTo(z / ZOOM_STEP)}>
            −
          </button>
          <span className="fe-zoom-value">{Math.round(z * 100)}%</span>
          <button type="button" className="fe-zoom-btn" aria-label={t('frames.tl.zoomIn')} title={t('frames.tl.zoomIn')} disabled={z >= ZOOM_MAX - 1e-6} onClick={() => zoomTo(z * ZOOM_STEP)}>
            +
          </button>
          <button type="button" className="fe-zoom-btn fe-zoom-btn--wide" disabled={z === 1} onClick={() => zoomTo(1)}>
            {t('frames.fit')}
          </button>
        </div>
      </div>
      <div ref={boxRef} className={`fe-tl-scroll ${moving ? 'fe-tl-scroll--moving' : ''}`}>
        {width > 0 && (
          <div ref={trackRef} className="fe-tl-track" style={{ width: total * px }}>
            <div
              className="fe-tl-ruler"
              aria-hidden
              title={t('frames.tl.rulerHint')}
              onPointerDown={rulerDown}
              onPointerMove={rulerMove}
              onPointerUp={rulerUp}
              onPointerCancel={rulerUp}
            >
              {ticks.map((v) => (
                <span key={v} className="fe-tl-tick" style={{ left: v * px }}>
                  {sec(v)}
                </span>
              ))}
            </div>
            {Array.from({ length: n }, (_, i) => {
              const ms = Math.round(frameMs(timing, i));
              const cut = cutOf(source.order, i) + 1; // какой это кадр листа
              const name = t('frames.tl.frameOf', { n: i + 1, cut, ms });
              const bw = (starts[i + 1] - starts[i]) * px;
              return (
                <div
                  key={i}
                  className={`fe-tl-frame ${i === open ? 'fe-tl-frame--on' : ''} ${moving?.from === i ? 'fe-tl-frame--drag' : ''}`}
                  style={{ left: starts[i] * px, width: (starts[i + 1] - starts[i]) * px }}
                >
                  <button
                    type="button"
                    className={`fe-tl-block ${i === open ? 'fe-tl-block--on' : ''}`}
                    aria-label={name}
                    aria-pressed={i === open}
                    title={name}
                    onPointerDown={(e) => grab(e, false, i)}
                    onPointerMove={pull}
                    onPointerUp={drop}
                    onPointerCancel={cancel}
                    onClick={() => {
                      if (!dragged.current) onPick(i);
                    }}
                    onKeyDown={(e) => {
                      if (onDelete && (e.key === 'Delete' || e.key === 'Backspace')) {
                        e.preventDefault();
                        onDelete(i);
                      }
                    }}
                  >
                    <span className="fe-tl-thumb" style={{ width: (THUMB_H * box.w) / box.h }}>
                      <FrameThumb s={source} cell={i} box={box} />
                    </span>
                    <span className="fe-tl-n">{i + 1}</span>
                    <span className="fe-tl-ms">{ms}</span>
                    {bw >= CUT_NARROW && (
                      <span className={`fe-tl-cut ${bw >= CUT_WIDE ? '' : 'fe-tl-cut--stack'}`}>{t(bw >= CUT_WIDE ? 'frames.tl.cut' : 'frames.tl.cutShort', { n: cut })}</span>
                    )}
                  </button>
                  <span
                    className="fe-tl-edge"
                    title={t('frames.tl.edgeHint')}
                    onPointerDown={(e) => grab(e, true, i)}
                    onPointerMove={pull}
                    onPointerUp={drop}
                    onPointerCancel={cancel}
                  />
                  {onDelete && (
                    <button
                      type="button"
                      className="fe-tl-del"
                      aria-label={t('frames.cell.remove', { n: i + 1 })}
                      title={t('frames.cell.remove', { n: i + 1 })}
                      onClick={() => onDelete(i)}
                    >
                      ×
                    </button>
                  )}
                </div>
              );
            })}
            {dropAt != null && <div className="fe-tl-drop" style={{ left: starts[dropAt] * px }} aria-hidden />}
          </div>
        )}
      </div>
      <p className="fe-note">
        {t('frames.tl.hint')} {t('frames.tl.zoomHint')}
      </p>
    </section>
  );
}

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { animTravel, type AnimTravel } from './animTravel';
import { animBox, animGrid, type PortraitAnim } from './animTypes';

/* Проигрыватель анимации портрета: кадры атласа (cols×rows клеток fw×fh, по
   порядку чтения) сменяются таймером: шаг — anim.fps, кадр стоит столько
   шагов, сколько задано в anim.holds (нет — один). Фон блока сдвигается на
   клетку, React кадры не перерисовывает. Без дела таймеров нет.

   Размер: длинная сторона кадра — size × anim.scale (size — сторона головы
   портрета), пропорции кадра — как у клетки атласа. Где стоит блок, решает
   родитель (у портрета — низом к низу головы, по середине).

   loop — играть по кругу, пока не размонтируют (шаги, «падает», «держат»);
   иначе anim.loops раз и onEnd. mirror — отразить по горизонтали (идёт в
   другую сторону, чем смотрит фигура в кадрах). view — окно, чьи таймеры
   брать (PiP).

   travel — фигура шагает внутри кадров (ходьба, animTravel.ts): следующий
   круг идёт с места последнего кадра, а не прыгает к месту первого.
   steady — фигура стоит в блоке на месте (сдвиг CSS translate), а её шаги
   уходят в onGait: окно портрета идёт ими (0 — шаги есть, null — нет);
   carry — фигура уходит дальше круг за кругом, вышла за край родителя —
   входит с другой стороны (превью). Шагов в кадрах нет — как без travel. */

const MIN_FRAME_MS = 16;

export type SpriteTravel = { mode: 'steady'; onGait: (step: number | null) => void } | { mode: 'carry' };

export interface AnimSpriteProps {
  anim: PortraitAnim;
  size: number;
  loop?: boolean;
  mirror?: boolean;
  onEnd?: () => void;
  /** Кадр сменился — номер кадра (для маски попадания) */
  onFrame?: (frame: number) => void;
  view?: Window;
  travel?: SpriteTravel;
  className?: string;
  style?: CSSProperties;
}

export default function AnimSprite({ anim, size, loop = false, mirror = false, onEnd, onFrame, view, travel, className, style }: AnimSpriteProps) {
  const ref = useRef<HTMLDivElement>(null);
  const { w, h } = animBox(anim, size);
  const grid = animGrid(anim);
  const frames = Math.max(1, Math.min(anim.frames, grid.rows * grid.cols));
  const onEndRef = useRef(onEnd);
  onEndRef.current = onEnd;
  const onFrameRef = useRef(onFrame);
  onFrameRef.current = onFrame;

  // Шаги фигуры в кадрах — по атласу, когда они нужны
  const mode = travel?.mode ?? null;
  const onGaitRef = useRef(travel?.mode === 'steady' ? travel.onGait : undefined);
  onGaitRef.current = travel?.mode === 'steady' ? travel.onGait : undefined;
  const [track, setTrack] = useState<AnimTravel | null>(null);
  const trackRef = useRef(track);
  trackRef.current = track;
  useEffect(() => {
    setTrack(null);
    if (!mode) return;
    let alive = true;
    animTravel(anim)
      .then((t) => alive && setTrack(t))
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- анимация по версии, не по объекту
  }, [mode, anim.id, anim.updated_at, anim.frames, anim.atlas]);
  // Шаги есть — окну идти ими; ушли (другая анимация, размонтировали) — ровно
  useEffect(() => {
    if (mode !== 'steady' || !track) return;
    onGaitRef.current?.(0);
    return () => onGaitRef.current?.(null);
  }, [mode, track]);

  const cols = anim.cols;
  const k = w / Math.max(1, anim.fw); // px блока на px кадра
  const dir = mirror ? -1 : 1;
  const at = useRef({ i: 0, n: 0 }); // какой кадр, какой по счёту с начала
  // Где блок: steady — фигура на своей средней середине; carry — круг дальше
  const place = (el: HTMLDivElement, i: number, n: number) => {
    const tr = mode ? trackRef.current : null;
    if (!tr) {
      el.style.translate = '';
      return;
    }
    let x: number;
    if (mode === 'steady') x = (tr.mid - (tr.at[i] ?? tr.mid)) * k * dir;
    else {
      // Сдвиг меняется только с новым кругом; блок целиком ушёл за край — с другой стороны
      const period = (el.parentElement?.clientWidth ?? w) + w;
      const far = Math.floor(n / frames) * tr.cycle * k * dir;
      x = ((((far + period / 2) % period) + period) % period) - period / 2;
    }
    el.style.translate = `${Math.round(x)}px 0`;
  };
  // Кадр i: фон блока — на его клетку атласа
  const show = (el: HTMLDivElement | null, i: number, n: number) => {
    if (!el) return;
    at.current = { i, n };
    el.style.backgroundPosition = `${-(i % cols) * w}px ${-Math.floor(i / cols) * h}px`;
    place(el, i, n);
    onFrameRef.current?.(i);
  };
  // Кадр prev сменился следующим — окну шаг (вперёд — по ходу фигуры в кадрах)
  const step = (prev: number) => {
    const tr = trackRef.current;
    if (mode === 'steady' && tr) onGaitRef.current?.((tr.steps[prev] ?? 0) * k * Math.sign(tr.cycle));
  };
  const showRef = useRef(show);
  const stepRef = useRef(step);
  useLayoutEffect(() => {
    showRef.current = show;
    stepRef.current = step;
  });

  // Первый кадр — до отрисовки, без мигания пустым блоком
  useLayoutEffect(() => showRef.current(ref.current, 0, 0), [anim.id, anim.updated_at, w, h]);
  // Шаги посчитались (или сменилось отражение) — блок сразу на место
  useLayoutEffect(() => {
    if (ref.current) place(ref.current, at.current.i, at.current.n);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- place читает то же
  }, [track, mode, dir, k]);

  useEffect(() => {
    const win = view ?? window;
    const step = 1000 / Math.max(1, Math.min(30, anim.fps));
    const holds = anim.holds;
    const hold = (i: number) => Math.max(MIN_FRAME_MS, step * (holds?.[i] ?? 1));
    const total = loop ? Infinity : frames * Math.max(1, anim.loops);
    let n = 0;
    let timer = 0;
    const tick = () => {
      const prev = n % frames;
      n += 1;
      if (n >= total) {
        onEndRef.current?.();
        return;
      }
      showRef.current(ref.current, n % frames, n);
      stepRef.current(prev);
      timer = win.setTimeout(tick, hold(n % frames));
    };
    // Один кадр без цикла — показать столько, сколько шёл бы кадр
    timer = win.setTimeout(tick, hold(0));
    return () => win.clearTimeout(timer);
    // Новый размер или анимация — новый цикл
  }, [anim.id, anim.updated_at, anim.fps, anim.holds, anim.loops, frames, loop, w, h, view]);

  return (
    <div
      ref={ref}
      className={className ? `anim-sprite ${className}` : 'anim-sprite'}
      role="img"
      aria-label={anim.name}
      style={{
        width: w,
        height: h,
        backgroundImage: anim.atlas ? `url(${anim.atlas})` : undefined,
        backgroundSize: `${grid.cols * w}px ${grid.rows * h}px`,
        backgroundRepeat: 'no-repeat',
        transform: mirror ? 'scaleX(-1)' : undefined,
        ...style,
      }}
    />
  );
}

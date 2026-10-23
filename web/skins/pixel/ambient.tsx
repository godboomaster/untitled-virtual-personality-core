import { useEffect, useRef } from 'react';
import type { SkinAmbientProps } from '../types';

/* Живой фон «Пиксель-арт»: медленно всплывающие пиксельные искры —
   квадратики из палитры «зелий» (днём — пыльца над равниной, ночью —
   светлячки подземелья с крестиком-ореолом). Координаты округляются до
   целых пикселей — искры остаются чёткими, без размытия. Мерцание —
   плавное, период 6–10 с; ≤30 кадров/с, пауза при скрытой вкладке,
   при reducedMotion — один статичный кадр. */

const LIGHT = ['#f2c84b', '#f26b5e', '#9dcb80', '#3fa9a0', '#f2b66d', '#c42f4f'];
const DARK = ['#f5c451', '#52e2f2', '#9dcb80', '#f2b66d', '#b79bf2'];
const SIZES = [3, 3, 4, 4, 4, 6];
const FRAME_MS = 1000 / 30;
const MAX_MOTES = 64;

interface Mote {
  x: number;
  y: number;
  size: number;
  speed: number; // px/с вверх
  sway: number; // амплитуда покачивания
  swayPeriod: number; // с
  twinkle: number; // период мерцания, с
  phase: number;
  color: string;
}

function rand(a: number, b: number): number {
  return a + Math.random() * (b - a);
}

function pick<T>(list: T[]): T {
  return list[Math.floor(Math.random() * list.length)];
}

export default function Ambient({ scheme, reducedMotion }: SkinAmbientProps) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;

    const dark = scheme === 'dark';
    const palette = dark ? DARK : LIGHT;
    const baseAlpha = dark ? 0.8 : 0.5;
    let w = 0;
    let h = 0;
    let motes: Mote[] = [];

    const spawn = (anywhere: boolean): Mote => ({
      x: rand(0, w),
      y: anywhere ? rand(0, h) : h + rand(6, 60),
      size: pick(SIZES),
      speed: rand(5, 13),
      sway: rand(4, 16),
      swayPeriod: rand(12, 22),
      twinkle: rand(6, 10),
      phase: rand(0, Math.PI * 2),
      color: pick(palette),
    });

    const resize = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.imageSmoothingEnabled = false;
      const n = Math.min(MAX_MOTES, Math.max(14, Math.round((w * h) / 24000)));
      if (motes.length > n) motes = motes.slice(0, n);
      while (motes.length < n) motes.push(spawn(true));
    };

    const draw = (tSec: number) => {
      ctx.clearRect(0, 0, w, h);
      for (const m of motes) {
        const sway = Math.sin((tSec / m.swayPeriod) * Math.PI * 2 + m.phase) * m.sway;
        const x = Math.round(m.x + sway);
        const y = Math.round(m.y);
        const tw = 0.55 + 0.45 * Math.sin((tSec / m.twinkle) * Math.PI * 2 + m.phase);
        const s = m.size;
        ctx.fillStyle = m.color;
        // Ночью крупные искры — с крестиком-ореолом из пикселей
        if (dark && s >= 4) {
          ctx.globalAlpha = baseAlpha * tw * 0.3;
          ctx.fillRect(x - s, y, s * 3, s);
          ctx.fillRect(x, y - s, s, s * 3);
        }
        ctx.globalAlpha = baseAlpha * tw;
        ctx.fillRect(x, y, s, s);
      }
      ctx.globalAlpha = 1;
    };

    resize();

    if (reducedMotion) {
      draw(0);
      const redraw = () => {
        resize();
        draw(0);
      };
      window.addEventListener('resize', redraw);
      return () => window.removeEventListener('resize', redraw);
    }

    let raf = 0;
    let last = 0;
    let prevT = 0;

    const tick = (now: number) => {
      raf = requestAnimationFrame(tick);
      if (now - last < FRAME_MS) return;
      const dt = prevT ? Math.min(0.1, (now - prevT) / 1000) : 0;
      prevT = now;
      last = now;
      for (let i = 0; i < motes.length; i++) {
        const m = motes[i];
        m.y -= m.speed * dt;
        if (m.y < -m.size * 3) motes[i] = spawn(false);
      }
      draw(now / 1000);
    };

    const start = () => {
      if (!raf) {
        prevT = 0;
        raf = requestAnimationFrame(tick);
      }
    };

    const stop = () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };

    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    window.addEventListener('resize', resize);
    document.addEventListener('visibilitychange', onVisibility);
    if (!document.hidden) start();

    return () => {
      stop();
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [scheme, reducedMotion]);

  return <canvas ref={ref} style={{ display: 'block', width: '100%', height: '100%' }} />;
}

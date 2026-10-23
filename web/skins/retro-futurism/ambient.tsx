import { useEffect, useRef } from 'react';
import type { SkinAmbientProps } from '../types';

/* Живой фон Retro-Futurism: медленная звёздная пыль (точки с мягким
   «дыханием», без вспышек) и тёплые боке-искры, плывущие вверх, как на
   постере. Светлая схема — редкие пылинки и бледные пятна света.
   ≤ 30 кадров/с, DPR ≤ 2, пауза при скрытой вкладке, reducedMotion —
   один статичный кадр. */

type Star = { x: number; y: number; r: number; a: number; period: number; phase: number; tint: string; vx: number };
type Spark = { x: number; y: number; r: number; a: number; vy: number; sway: number; phase: number; sprite: number };

const FRAME_MS = 1000 / 30;

function rand(min: number, max: number) {
  return min + Math.random() * (max - min);
}

// Круглое мягкое пятно заранее — рисовать спрайт дешевле градиента в кадре
function makeSprite(rgb: string, size: number): HTMLCanvasElement {
  const c = document.createElement('canvas');
  c.width = size;
  c.height = size;
  const g = c.getContext('2d');
  if (g) {
    const h = size / 2;
    const grd = g.createRadialGradient(h, h, 0, h, h, h);
    grd.addColorStop(0, `rgba(${rgb},1)`);
    grd.addColorStop(0.35, `rgba(${rgb},0.55)`);
    grd.addColorStop(1, `rgba(${rgb},0)`);
    g.fillStyle = grd;
    g.fillRect(0, 0, size, size);
  }
  return c;
}

export default function Ambient({ scheme, reducedMotion }: SkinAmbientProps) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;

    const dark = scheme === 'dark';
    const sprites = dark
      ? [makeSprite('246,114,58', 128), makeSprite('242,185,80', 128), makeSprite('196,59,57', 128)]
      : [makeSprite('246,114,58', 128), makeSprite('80,163,171', 128), makeSprite('242,185,80', 128)];

    let w = 0;
    let h = 0;
    let stars: Star[] = [];
    let sparks: Spark[] = [];

    const seed = () => {
      const area = (w * h) / (1440 * 900);
      const nStars = Math.round((dark ? 56 : 30) * Math.min(1, Math.max(0.45, area)));
      const nSparks = dark ? 12 : 10;
      stars = Array.from({ length: nStars }, () => ({
        x: rand(0, w),
        y: rand(0, h),
        r: dark ? rand(0.5, 1.5) : rand(0.6, 1.3),
        a: dark ? rand(0.25, 0.75) : rand(0.1, 0.22),
        period: rand(7, 15),
        phase: rand(0, Math.PI * 2),
        tint: dark ? (Math.random() < 0.25 ? '132,205,210' : '250,226,200') : '29,49,60',
        vx: rand(-1.6, -0.4),
      }));
      sparks = Array.from({ length: nSparks }, () => ({
        x: rand(0, w),
        y: rand(h * 0.15, h * 1.05),
        r: rand(10, 34),
        a: dark ? rand(0.07, 0.16) : rand(0.05, 0.09),
        vy: rand(3, 8),
        sway: rand(6, 18),
        phase: rand(0, Math.PI * 2),
        sprite: Math.floor(rand(0, sprites.length)),
      }));
    };

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed();
    };

    const draw = (t: number) => {
      ctx.clearRect(0, 0, w, h);
      const sec = t / 1000;
      for (const s of stars) {
        const breathe = 0.72 + 0.28 * Math.sin((sec * Math.PI * 2) / s.period + s.phase);
        ctx.fillStyle = `rgba(${s.tint},${(s.a * breathe).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      for (const p of sparks) {
        const x = p.x + Math.sin(sec / 6 + p.phase) * p.sway;
        const d = p.r * 2;
        ctx.globalAlpha = p.a * (0.8 + 0.2 * Math.sin(sec / 5 + p.phase));
        ctx.drawImage(sprites[p.sprite], x - p.r, p.y - p.r, d, d);
      }
      ctx.globalAlpha = 1;
    };

    const step = (dt: number) => {
      for (const s of stars) {
        s.x += s.vx * dt;
        if (s.x < -2) s.x = w + 2;
      }
      for (const p of sparks) {
        p.y -= p.vy * dt;
        if (p.y < -p.r * 2) {
          p.y = h + p.r * 2;
          p.x = rand(0, w);
        }
      }
    };

    resize();

    if (reducedMotion) {
      draw(0);
      const onResize = () => {
        resize();
        draw(0);
      };
      window.addEventListener('resize', onResize);
      return () => window.removeEventListener('resize', onResize);
    }

    let raf = 0;
    let last = 0;
    let prev = 0;
    const loop = (t: number) => {
      raf = requestAnimationFrame(loop);
      if (t - last < FRAME_MS) return;
      const dt = prev ? Math.min((t - prev) / 1000, 0.1) : 0;
      prev = t;
      last = t;
      step(dt);
      draw(t);
    };
    const start = () => {
      if (!raf) {
        prev = 0;
        raf = requestAnimationFrame(loop);
      }
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };
    const onVisibility = () => (document.hidden ? stop() : start());

    window.addEventListener('resize', resize);
    document.addEventListener('visibilitychange', onVisibility);
    if (!document.hidden) start();

    return () => {
      stop();
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [scheme, reducedMotion]);

  return <canvas ref={ref} style={{ position: 'absolute', inset: 0, display: 'block' }} />;
}

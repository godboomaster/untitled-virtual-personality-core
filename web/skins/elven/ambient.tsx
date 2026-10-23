import { useEffect, useRef } from 'react';
import type { SkinAmbientProps } from '../types';

/* Живой фон «Эльфийской сказки».
   Тёмная схема — редкие «светлячки цветом»: крошечные чёткие точки мяты
   и крема (без ореолов и размытия) медленно всплывают и плавно меняют
   яркость (период 6–11 с). Светлая схема — печатный лист, фон неподвижен:
   ничего не рисуем.
   ≤30 кадров/с; пауза, пока вкладка скрыта; reducedMotion — один кадр. */

const COLORS = ['#d1f2e0', '#d1f2e0', '#e9d3a2'];
const FRAME_MS = 1000 / 30;
const MAX_DOTS = 36;

interface Dot {
  x: number;
  y: number;
  r: number; // радиус, px
  speed: number; // px/с вверх
  sway: number; // амплитуда покачивания, px
  swayPeriod: number; // с
  period: number; // период «дыхания», с
  phase: number;
  color: string;
}

function rand(a: number, b: number): number {
  return a + Math.random() * (b - a);
}

export default function Ambient({ scheme, reducedMotion }: SkinAmbientProps) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    if (scheme !== 'dark') {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }

    let w = 0;
    let h = 0;
    let dots: Dot[] = [];

    const spawn = (anywhere: boolean): Dot => ({
      x: rand(0, w),
      y: anywhere ? rand(0, h) : h + rand(4, 40),
      r: rand(0.9, 1.6),
      speed: rand(3, 8),
      sway: rand(4, 14),
      swayPeriod: rand(14, 26),
      period: rand(6, 11),
      phase: rand(0, 1),
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
    });

    const resize = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const n = Math.min(MAX_DOTS, Math.max(12, Math.round((w * h) / 36000)));
      if (dots.length > n) dots = dots.slice(0, n);
      while (dots.length < n) dots.push(spawn(true));
    };

    const draw = (t: number) => {
      ctx.clearRect(0, 0, w, h);
      for (const d of dots) {
        const x = d.x + Math.sin((t / d.swayPeriod) * Math.PI * 2 + d.phase * 6.28) * d.sway;
        // Плавное «дыхание»: долго тусклая, мягко светлеет — без вспышек
        const c = 0.5 - 0.5 * Math.cos(((t / d.period + d.phase) % 1) * Math.PI * 2);
        ctx.globalAlpha = 0.2 + 0.7 * c;
        ctx.fillStyle = d.color;
        ctx.beginPath();
        ctx.arc(x, d.y, d.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    };

    const step = (dt: number) => {
      for (let i = 0; i < dots.length; i++) {
        const d = dots[i];
        d.y -= d.speed * dt;
        if (d.y < -10) dots[i] = spawn(false);
      }
    };

    resize();
    let t = rand(0, 60);

    if (reducedMotion) {
      draw(t);
      const onResize = () => {
        resize();
        draw(t);
      };
      window.addEventListener('resize', onResize);
      return () => window.removeEventListener('resize', onResize);
    }

    let raf = 0;
    let last = 0;
    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      if (now - last < FRAME_MS) return;
      const dt = last ? Math.min(0.1, (now - last) / 1000) : 0;
      last = now;
      t += dt;
      step(dt);
      draw(t);
    };
    const start = () => {
      if (raf) return;
      last = 0;
      raf = requestAnimationFrame(frame);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      raf = 0;
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('resize', resize);
    if (!document.hidden) start();
    else draw(t);

    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('resize', resize);
    };
  }, [scheme, reducedMotion]);

  return <canvas ref={ref} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }} />;
}

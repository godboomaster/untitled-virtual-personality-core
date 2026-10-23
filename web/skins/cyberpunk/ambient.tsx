import { useEffect, useRef } from 'react';
import type { SkinAmbientProps } from '../types';

/* Киберпанк: живой фон — медленный неоновый дождь (тонкие косые штрихи
   розового, бирюзового и лавандового света, как отражения вывесок на
   мокром стекле) и широкая мягкая полоса развёртки, которая очень медленно
   сползает сверху вниз. Без вспышек: яркость каждого штриха постоянна.
   ~30 кадров/с, пауза в фоне, при reducedMotion — один статичный кадр. */

interface Streak {
  x: number;
  y: number;
  len: number;
  speed: number; // px/с
  alpha: number;
  rgb: string;
  width: number;
}

const PALETTE = ['255, 46, 136', '49, 214, 224', '255, 46, 136', '49, 214, 224', '172, 154, 204'];
const SLANT = 0.16; // наклон штриха: dx на 1px падения
const BAND_PERIOD = 18; // с — полный проход полосы развёртки
const FRAME_MS = 1000 / 30;

function rand(a: number, b: number): number {
  return a + Math.random() * (b - a);
}

export default function Ambient({ reducedMotion }: SkinAmbientProps) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;

    let w = 0;
    let h = 0;
    let streaks: Streak[] = [];
    let raf = 0;
    let last = 0;
    let time = 0;

    const spawn = (anywhere: boolean): Streak => {
      const len = rand(36, 150);
      return {
        x: rand(-40, w + 80),
        y: anywhere ? rand(-len, h) : rand(-h * 0.3, -len),
        len,
        speed: rand(26, 64),
        alpha: rand(0.07, 0.22),
        rgb: PALETTE[Math.floor(Math.random() * PALETTE.length)],
        width: Math.random() < 0.18 ? 1.6 : 1,
      };
    };

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.max(22, Math.min(64, Math.round((w * h) / 21000)));
      streaks = Array.from({ length: count }, () => spawn(true));
    };

    const draw = () => {
      ctx.clearRect(0, 0, w, h);

      // Полоса развёртки: широкая, едва заметная, без резких краёв
      const bandH = Math.max(160, h * 0.22);
      const bandY = ((time / BAND_PERIOD) % 1) * (h + bandH * 2) - bandH;
      const band = ctx.createLinearGradient(0, bandY, 0, bandY + bandH);
      band.addColorStop(0, 'rgba(49, 214, 224, 0)');
      band.addColorStop(0.5, 'rgba(49, 214, 224, 0.035)');
      band.addColorStop(1, 'rgba(49, 214, 224, 0)');
      ctx.fillStyle = band;
      ctx.fillRect(0, bandY, w, bandH);

      // Неоновый дождь: хвост гаснет кверху, голова — ровного цвета
      ctx.lineCap = 'round';
      for (const s of streaks) {
        const x2 = s.x - s.len * SLANT;
        const y2 = s.y + s.len;
        const g = ctx.createLinearGradient(s.x, s.y, x2, y2);
        g.addColorStop(0, `rgba(${s.rgb}, 0)`);
        g.addColorStop(1, `rgba(${s.rgb}, ${s.alpha})`);
        ctx.strokeStyle = g;
        ctx.lineWidth = s.width;
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
    };

    const step = (dt: number) => {
      time += dt;
      for (let i = 0; i < streaks.length; i++) {
        const s = streaks[i];
        s.y += s.speed * dt;
        s.x -= s.speed * dt * SLANT;
        if (s.y > h + 10 || s.x < -200) streaks[i] = spawn(false);
      }
    };

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      if (!last) last = now;
      const elapsed = now - last;
      if (elapsed < FRAME_MS) return;
      last = now;
      step(Math.min(elapsed, 100) / 1000);
      draw();
    };

    const start = () => {
      if (raf || reducedMotion) return;
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

    const onResize = () => {
      resize();
      draw();
    };

    resize();
    time = BAND_PERIOD * 0.35; // статичный кадр — полоса в верхней трети
    draw();
    if (!reducedMotion && !document.hidden) start();
    window.addEventListener('resize', onResize);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [reducedMotion]);

  return <canvas ref={ref} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', display: 'block' }} />;
}

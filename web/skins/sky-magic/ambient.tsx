import { useEffect, useRef } from 'react';
import type { SkinAmbientProps } from '../types';

/* Живой фон «Волшебного неба»: редкие звёзды на плоском небе, как в
   книжной иллюстрации. Два вида знаков — чёткие точки и четырёхлучевые
   искры (сплошная фигура без ореола). Звёзды не движутся, раскладку задаёт
   детерминированный генератор; каждая звезда очень медленно и мягко
   меняет яркость (период 6–14 с), искры чуть «дышат» размером.
   Искры не садятся на текст неба: раз в ~0,7 с фон смотрит, где
   шапка, заголовки разделов, герой главной и т. п., — искра под ними
   плавно гаснет (≈0,5 с) и загорается в свободном месте. Всего знаков ≤ 76;
   ≤ 20 кадров/с, пауза при скрытой вкладке, при reducedMotion — один
   статичный кадр (перерисовывается только при смене раскладки). */

interface Star {
  x: number; // доля ширины
  y: number; // доля высоты
  r: number; // радиус, CSS px
  spark: boolean;
  color: string;
  alpha: number;
  period: number; // с
  phase: number;
  vis: number; // 0…1 — видимость искры (гаснет над текстом)
  blocked: boolean;
}

interface Box {
  l: number;
  t: number;
  r: number;
  b: number;
}

const FRAME_MS = 1000 / 20;
const LAYOUT_MS = 700;
const TAU = Math.PI * 2;

// Где на небе текст — там искр не бывает (точки — можно, они крошечные)
const KEEP_OUT = [
  '.topbar',
  '.sidebar',
  '.home-hero',
  '.section-header',
  '.home-block-head',
  '.chat-overview-head',
  '.chat-header',
  '.chat-status-bar',
  '.chat-input-bar',
  '.start-guide-lead',
  '.home-foot-stencil',
  '.home-foot-right',
  '.voice-chat-topbar',
  '.boot-panel',
].join(',');

// Ночь: кремовые и сланцевые точки, янтарные и кремовые искры
const NIGHT = { dots: ['#f1e7d0', '#f1e7d0', '#8ea5cc'], sparks: ['#e9a35b', '#e9a35b', '#f1e7d0'], dotAlpha: 0.75, sparkAlpha: 0.95 };
// День: белые точки и искры
const DAY = { dots: ['#fffaf0', '#fffaf0', '#e4e8fb'], sparks: ['#fffaf0'], dotAlpha: 0.7, sparkAlpha: 0.92 };

// Детерминированный генератор (mulberry32)
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Четырёхлучевая искра: восемь вершин, внутренние — на диагоналях
function sparkPath(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  const k = r * 0.17;
  ctx.beginPath();
  ctx.moveTo(x, y - r);
  ctx.lineTo(x + k, y - k);
  ctx.lineTo(x + r, y);
  ctx.lineTo(x + k, y + k);
  ctx.lineTo(x, y + r);
  ctx.lineTo(x - k, y + k);
  ctx.lineTo(x - r, y);
  ctx.lineTo(x - k, y - k);
  ctx.closePath();
}

export default function Ambient({ scheme, reducedMotion }: SkinAmbientProps) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;

    const pal = scheme === 'dark' ? NIGHT : DAY;
    const rand = rng(20261021);
    const pick = <T,>(list: T[]) => list[Math.floor(rand() * list.length)];
    let w = 0;
    let h = 0;
    let stars: Star[] = [];
    let boxes: Box[] = [];

    const isBlocked = (s: Star) => {
      const x = s.x * w;
      const y = s.y * h;
      const pad = s.r + 6;
      for (const b of boxes) {
        if (x > b.l - pad && x < b.r + pad && y > b.t - pad && y < b.b + pad) return true;
      }
      return false;
    };

    // Новое место для погасшей искры: несколько попыток найти свободное небо
    const relocate = (s: Star) => {
      for (let k = 0; k < 12; k++) {
        s.x = rand();
        s.y = rand();
        if (!isBlocked(s)) return true;
      }
      return false;
    };

    const measure = () => {
      boxes = [];
      for (const el of document.querySelectorAll(KEEP_OUT)) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < h) {
          boxes.push({ l: r.left, t: r.top, r: r.right, b: r.bottom });
        }
      }
      for (const s of stars) if (s.spark) s.blocked = isBlocked(s);
    };

    const build = () => {
      const area = w * h;
      const nDots = Math.min(62, Math.max(18, Math.round(area / 17000)));
      const nSparks = Math.min(14, Math.max(4, Math.round(area / 95000)));
      stars = [];
      for (let i = 0; i < nDots; i++) {
        stars.push({
          x: rand(),
          y: rand(),
          r: rand() < 0.8 ? 0.9 + rand() * 0.5 : 1.6 + rand() * 0.5,
          spark: false,
          color: pick(pal.dots),
          alpha: pal.dotAlpha * (0.55 + rand() * 0.45),
          period: 6 + rand() * 8,
          phase: rand() * TAU,
          vis: 1,
          blocked: false,
        });
      }
      for (let i = 0; i < nSparks; i++) {
        stars.push({
          x: rand(),
          y: rand(),
          r: rand() < 0.7 ? 4 + rand() * 2.5 : 7 + rand() * 3,
          spark: true,
          color: pick(pal.sparks),
          alpha: pal.sparkAlpha,
          period: 8 + rand() * 6,
          phase: rand() * TAU,
          vis: 1,
          blocked: false,
        });
      }
    };

    // Статичный кадр: искры сразу на свободных местах
    const settle = () => {
      measure();
      for (const s of stars) {
        if (!s.spark) continue;
        if (s.blocked && relocate(s)) s.blocked = false;
        s.vis = s.blocked ? 0 : 1;
      }
    };

    const resize = () => {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      build();
      settle();
    };

    const draw = (tSec: number, still: boolean) => {
      ctx.clearRect(0, 0, w, h);
      for (const s of stars) {
        if (s.vis <= 0.01) continue;
        // Мягкая волна яркости: от 55 до 100 % — без вспышек и погасаний
        const wave = still ? 1 : 0.775 + 0.225 * Math.sin((tSec / s.period) * TAU + s.phase);
        ctx.globalAlpha = s.alpha * wave * s.vis;
        ctx.fillStyle = s.color;
        const x = s.x * w;
        const y = s.y * h;
        if (s.spark) {
          const scale = still ? 1 : 0.9 + 0.1 * Math.sin((tSec / s.period) * TAU + s.phase);
          sparkPath(ctx, x, y, s.r * scale);
          ctx.fill();
        } else {
          ctx.beginPath();
          ctx.arc(x, y, s.r, 0, TAU);
          ctx.fill();
        }
      }
      ctx.globalAlpha = 1;
    };

    resize();

    if (reducedMotion) {
      // Один кадр; перерисовка — только когда меняется раскладка экрана
      const redraw = () => {
        settle();
        draw(0, true);
      };
      const onResize = () => {
        resize();
        draw(0, true);
      };
      draw(0, true);
      const timer = window.setInterval(() => {
        if (!document.hidden) redraw();
      }, LAYOUT_MS * 2);
      window.addEventListener('resize', onResize);
      return () => {
        window.clearInterval(timer);
        window.removeEventListener('resize', onResize);
      };
    }

    let raf = 0;
    let last = 0;
    let lastLayout = 0;

    const tick = (now: number) => {
      raf = requestAnimationFrame(tick);
      if (now - last < FRAME_MS) return;
      const dt = last ? Math.min(0.2, (now - last) / 1000) : 0;
      last = now;
      if (now - lastLayout > LAYOUT_MS) {
        lastLayout = now;
        measure();
      }
      // Искра над текстом плавно гаснет (≈0,5 с), на новом месте — загорается (≈2 с)
      for (const s of stars) {
        if (!s.spark) continue;
        if (s.blocked) {
          s.vis = Math.max(0, s.vis - dt * 2);
          if (s.vis === 0 && relocate(s)) s.blocked = false;
        } else if (s.vis < 1) {
          s.vis = Math.min(1, s.vis + dt * 0.5);
        }
      }
      draw(now / 1000, false);
    };

    const start = () => {
      if (!raf) {
        last = 0;
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

    const onResize = () => {
      resize();
      draw(performance.now() / 1000, false);
    };

    window.addEventListener('resize', onResize);
    document.addEventListener('visibilitychange', onVisibility);
    if (!document.hidden) start();

    return () => {
      stop();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [scheme, reducedMotion]);

  return <canvas ref={ref} style={{ display: 'block', width: '100%', height: '100%' }} />;
}

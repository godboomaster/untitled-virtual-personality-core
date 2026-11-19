import { useEffect, useRef, useState } from 'react';

/* Погладить живой портрет: PAT_MS после последнего касания глаза закрыты и
   голова под руку (petted у LivePortrait), голова чуть сплющивается, из
   места касания всплывают сердечки (PatHearts) — как у аватара поверх
   приложений на телефоне (AvatarHeadView.java): вспыхивают с перескоком
   размера, плывут вверх, покачиваясь, и гаснут; иногда рядом — второе,
   поменьше. Одно на всех: портрет на рабочем столе (desktop/src/portrait/),
   голосовой режим и PiP-окно веба. Куда гладить — решает вызывающий (по
   маске фигуры, hitMask). */

const PAT_MS = 1100;
export const HEART_MS = 1100;
const HEARTS_MAX = 6;
const HEART_COLORS = ['#e5484d', '#f06a7a'];

const SQUASH: Keyframe[] = [
  { transform: 'scale(1, 1)' },
  { transform: 'scale(1.035, 0.94)', offset: 0.35 },
  { transform: 'scale(0.99, 1.015)', offset: 0.7 },
  { transform: 'scale(1, 1)' },
];

/** Сердечко: середина (в слое сердечек), размер, на сколько всплывёт и
 *  качнётся, фаза покачивания, цвет, задержка перед вспышкой — всё в px и мс */
export interface PatHeart {
  id: number;
  x: number;
  y: number;
  size: number;
  rise: number;
  sway: number;
  phase: number;
  color: string;
  delay: number;
}

const rand = (a: number, b: number) => a + Math.random() * (b - a);

/** Сердечки на одно касание в точке (x, y): одно, в 40 % — ещё одно рядом,
 *  поменьше и чуть позже. Чуть выше точки (палец или курсор не закрывает);
 *  у верха слоя всплывают ниже — сколько есть места */
export function heartsAt(x: number, y: number, nextId: () => number): PatHeart[] {
  const n = Math.random() < 0.4 ? 2 : 1;
  const out: PatHeart[] = [];
  for (let i = 0; i < n; i++) {
    const small = i > 0;
    const size = small ? rand(12, 15) : rand(18, 23);
    const hx = x + (small ? (Math.random() < 0.5 ? -1 : 1) * rand(14, 22) : rand(-5, 5));
    const hy = Math.max(size * 1.2, y - size * 0.7);
    out.push({
      id: nextId(),
      x: hx,
      y: hy,
      size,
      rise: Math.max(size * 0.6, Math.min(small ? 30 : 42, hy - size * 0.6)),
      sway: rand(3, 6),
      phase: rand(0, Math.PI * 2),
      color: HEART_COLORS[Math.floor(Math.random() * HEART_COLORS.length)],
      delay: small ? rand(90, 180) : 0,
    });
  }
  return out;
}

export function usePat(onPat?: () => void) {
  const [petted, setPetted] = useState(false);
  const [hearts, setHearts] = useState<PatHeart[]>([]);
  const timer = useRef(0);
  const heartId = useRef(0);
  const cb = useRef(onPat);
  cb.current = onPat;

  useEffect(() => () => window.clearTimeout(timer.current), []);

  /** Погладили в точке (x, y) — координаты в слое сердечек; portrait — .live-portrait (сплющить). */
  const pat = (x: number, y: number, portrait?: Element | null) => {
    setPetted(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setPetted(false), PAT_MS);
    portrait?.animate(SQUASH, { duration: 320, easing: 'ease-out' });
    const added = heartsAt(x, y, () => ++heartId.current);
    setHearts((h) => [...h, ...added].slice(-HEARTS_MAX));
    for (const a of added) {
      window.setTimeout(() => setHearts((h) => h.filter((v) => v.id !== a.id)), HEART_MS + a.delay + 50);
    }
    cb.current?.();
  };

  return { petted, hearts, pat };
}

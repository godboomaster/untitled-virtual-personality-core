import { useEffect, useRef, useState } from 'react';

/* Погладить живой портрет: PAT_MS после последнего касания глаза закрыты и
   голова под руку (petted у LivePortrait), голова чуть сплющивается, над
   рукой всплывает сердечко. Одно на всех: портрет на рабочем столе
   (desktop/src/portrait/), голосовой режим и PiP-окно веба. Куда гладить —
   решает вызывающий (по маске фигуры, hitMask). */

const PAT_MS = 1100;
const HEART_MS = 900;
const HEARTS_MAX = 6;

const SQUASH: Keyframe[] = [
  { transform: 'scale(1, 1)' },
  { transform: 'scale(1.035, 0.94)', offset: 0.35 },
  { transform: 'scale(0.99, 1.015)', offset: 0.7 },
  { transform: 'scale(1, 1)' },
];

export interface PatHeart {
  id: number;
  x: number;
  y: number;
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
    const id = ++heartId.current;
    setHearts((h) => [...h.slice(-(HEARTS_MAX - 1)), { id, x, y }]);
    window.setTimeout(() => setHearts((h) => h.filter((v) => v.id !== id)), HEART_MS);
    cb.current?.();
  };

  return { petted, hearts, pat };
}

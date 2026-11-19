import { useLayoutEffect, useRef } from 'react';
import { HEART_MS, type PatHeart } from './usePat';

/* Сердечки над рукой (.portrait-heart, слой — position: relative или fixed).
   Движение — как у аватара на телефоне (AvatarHeadView.drawHearts): вспышка
   0,4 → 1,15 → 1, всплытие с замедлением, покачивание вбок с поворотом
   ±9°, к концу гаснет. Ключевые кадры считаются заранее (WAAPI: только
   transform и opacity — без перерисовки страницы), между ними — линейно. */

const STEPS = 24;

const easeOut = (p: number) => 1 - Math.pow(1 - Math.min(1, Math.max(0, p)), 3);

function heartFrames(h: PatHeart): Keyframe[] {
  const frames: Keyframe[] = [];
  for (let i = 0; i <= STEPS; i++) {
    const p = i / STEPS;
    const scale = p < 0.22 ? 0.4 + 0.75 * easeOut(p / 0.22) : p < 0.38 ? 1.15 - 0.15 * ((p - 0.22) / 0.16) : 1;
    const alpha = p < 0.12 ? p / 0.12 : p < 0.6 ? 1 : 1 - (p - 0.6) / 0.4;
    const wobble = Math.sin(p * Math.PI * 2 * 0.9 + h.phase);
    const dx = wobble * h.sway * (1 - 0.3 * p);
    const dy = -easeOut(p) * h.rise;
    frames.push({
      offset: p,
      opacity: Math.max(0, Math.min(1, alpha)).toFixed(3),
      transform: `translate(${dx.toFixed(2)}px, ${dy.toFixed(2)}px) rotate(${(wobble * 9).toFixed(2)}deg) scale(${scale.toFixed(3)})`,
    });
  }
  return frames;
}

function Heart({ h }: { h: PatHeart }) {
  const ref = useRef<SVGSVGElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const view = el.ownerDocument.defaultView ?? window;
    const calm = view.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const anim = el.animate(calm ? [{ opacity: 1 }, { opacity: 0 }] : heartFrames(h), {
      duration: HEART_MS,
      delay: h.delay,
      fill: 'both',
      easing: 'linear',
    });
    return () => anim.cancel();
    // Одно сердечко — один объект на всю жизнь: анимация не перезапускается
  }, [h]);
  return (
    <svg
      ref={ref}
      className="portrait-heart"
      style={{
        left: h.x,
        top: h.y,
        width: h.size,
        height: h.size,
        marginLeft: -h.size / 2,
        marginTop: -h.size / 2,
        fill: h.color,
      }}
      viewBox="0 0 24 24"
      aria-hidden
    >
      <path d="M12 21 2.6 11.6A5.6 5.6 0 0 1 12 3.9a5.6 5.6 0 0 1 9.4 7.7Z" />
    </svg>
  );
}

/** Сердечки над рукой: каждое — своё всплытие (heartFrames) */
export default function PatHearts({ hearts }: { hearts: PatHeart[] }) {
  return (
    <>
      {hearts.map((h) => (
        <Heart key={h.id} h={h} />
      ))}
    </>
  );
}

import type { PatHeart } from './usePat';

/** Сердечки над рукой (.portrait-heart, слой — position: relative или fixed). */
export default function PatHearts({ hearts }: { hearts: PatHeart[] }) {
  return (
    <>
      {hearts.map((h) => (
        <svg key={h.id} className="portrait-heart" style={{ left: h.x, top: h.y }} viewBox="0 0 24 22" aria-hidden>
          <path d="M12 21 2.6 11.6A5.6 5.6 0 0 1 12 3.9a5.6 5.6 0 0 1 9.4 7.7Z" />
        </svg>
      ))}
    </>
  );
}

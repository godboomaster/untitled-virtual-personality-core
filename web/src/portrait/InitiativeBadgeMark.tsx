import type { InitiativeBadge } from './portraitTypes';
import './initiativeBadgeMark.css';

export const BADGE_MIN_SIZE = 24;
export const BADGE_MAX_SIZE = 72;
export const BADGE_DEFAULT_SIZE = 34;

export function initiativeBadgeSize(badge?: InitiativeBadge | null): number {
  const size = badge?.size ?? BADGE_DEFAULT_SIZE;
  return Number.isInteger(size) && size >= BADGE_MIN_SIZE && size <= BADGE_MAX_SIZE ? size : BADGE_DEFAULT_SIZE;
}

// Векторные символы выровнены по видимому контуру, без отступов шрифта.
const PRESET_PATHS: Record<string, string> = {
  '!': 'M9.5 1h5v15h-5zM9.5 18h5v5h-5z',
  '★': 'M12 1l3.4 7.25 8.1.85-6 5.5 1.8 8.4-7.3-4.1L4.7 23l1.8-8.4-6-5.5 8.1-.85Z',
  '✦': 'M12 0C13.5 7.2 16.8 10.5 24 12C16.8 13.5 13.5 16.8 12 24C10.5 16.8 7.2 13.5 0 12C7.2 10.5 10.5 7.2 12 0Z',
};

export default function InitiativeBadgeMark({ badge, label, className = '' }: {
  badge?: InitiativeBadge | null;
  label: string;
  className?: string;
}) {
  const size = initiativeBadgeSize(badge);
  const symbol = badge?.symbol || '!';
  const path = Object.hasOwn(PRESET_PATHS, symbol) ? PRESET_PATHS[symbol] : undefined;
  const image = badge?.kind === 'image' ? badge.image : null;
  return (
    <span className={`initiative-badge-mark ${className}`} style={{ width: size, height: size }} role="img" aria-label={label}>
      {image ? <img src={image} alt="" /> : path ? (
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d={path} fill="currentColor" /></svg>
      ) : (
        <span style={{ fontSize: size * Math.min(0.8, 0.8 / Array.from(symbol).length) }} aria-hidden="true">{symbol}</span>
      )}
    </span>
  );
}

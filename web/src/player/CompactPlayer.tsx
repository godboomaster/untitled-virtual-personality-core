import { useEffect, useState, type RefObject } from 'react';
import Icon, { type IconName } from '../components/icons';
import { useI18n } from '../i18n';

function clock(value: number) {
  const n = Math.max(0, Math.floor(Number.isFinite(value) ? value : 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
}
const rangeKeys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];

/** The web footer only; the avatar has its own independent mini player. */
export default function CompactPlayer({ title, subtitle, playing, busy = false, position, duration, volume,
  previousDisabled, nextDisabled, onToggle, onPrevious, onNext, onSeek, onVolume, onOpen, onClose }: {
  title: string; subtitle: string; playing: boolean; busy?: boolean;
  position: number; duration: number; volume: number; previousDisabled?: boolean; nextDisabled?: boolean;
  onToggle: () => void; onPrevious: () => void; onNext: () => void;
  onSeek: (value: number) => void | Promise<void>; onVolume: (value: number) => void | Promise<void>;
  onOpen: () => void; onClose: () => void;
}) {
  const { t } = useI18n();
  const [seek, setSeek] = useState<number | null>(null);
  const [level, setLevel] = useState<number | null>(null);
  const button = (icon: IconName, label: string, action: () => void, disabled = false) =>
    <button type="button" className="btn music-compact-button" title={label} aria-label={label} disabled={busy || disabled} onClick={action}><Icon name={icon} size={17} /></button>;
  const commitSeek = async (value: number) => { try { await onSeek(value); } finally { setSeek(null); } };
  const commitVolume = async (value: number) => { try { await onVolume(value / 100); } finally { setLevel(null); } };
  return <div className="music-compact" role="group" aria-label={t('nav.player')}>
    <div className="music-compact-info"><strong title={title}>{title}</strong><span title={subtitle}>{subtitle}</span></div>
    <div className="music-compact-transport">
      {button('chevronLeft', t('player.previous'), onPrevious, previousDisabled)}
      {button(playing ? 'pause' : 'start', t(playing ? 'player.browser.pause' : 'player.browser.resume'), onToggle)}
      {button('chevronRight', t('player.next'), onNext, nextDisabled)}
    </div>
    <div className="music-compact-timeline">
      <input type="range" aria-label={t('player.browser.seek')} min="0" max={Math.max(1, Math.floor(duration))}
        value={seek ?? Math.min(Math.floor(position), Math.max(1, Math.floor(duration)))} disabled={busy || duration <= 0}
        onChange={(e) => setSeek(Number(e.target.value))} onPointerUp={(e) => { void commitSeek(Number(e.currentTarget.value)); }}
        onKeyUp={(e) => { if (rangeKeys.includes(e.key)) void commitSeek(Number(e.currentTarget.value)); }} />
      <span>{clock(seek ?? position)}{duration > 0 ? ` / ${clock(duration)}` : ''}</span>
    </div>
    <label className="music-compact-volume" title={`${t('player.volume')} · ${level ?? Math.round(volume * 100)}%`}>
      <Icon name="volume" size={16} />
      <input type="range" aria-label={t('player.volume')} min="0" max="100" value={level ?? Math.round(volume * 100)} disabled={busy}
        onChange={(e) => setLevel(Number(e.target.value))} onPointerUp={(e) => { void commitVolume(Number(e.currentTarget.value)); }}
        onKeyUp={(e) => { if (rangeKeys.includes(e.key)) void commitVolume(Number(e.currentTarget.value)); }} />
    </label>
    <div className="music-compact-actions">
      {button('popout', t('player.mini.expand'), onOpen)}
      {button('close', t('player.mini.close'), onClose)}
    </div>
  </div>;
}

export function LocalCompactPlayer({ audio, title, index, count, loading, onPrevious, onNext, onOpen, onClose, onError }: {
  audio: RefObject<HTMLAudioElement | null>; title: string; index: number; count: number; loading: boolean;
  onPrevious: () => void; onNext: () => void; onOpen: () => void; onClose: () => void; onError: () => void;
}) {
  const { t } = useI18n();
  const [state, setState] = useState({ playing: false, position: 0, duration: 0, volume: .5 });
  useEffect(() => {
    const el = audio.current;
    if (!el) return;
    const update = () => setState({ playing: !el.paused && !el.ended, position: el.currentTime,
      duration: Number.isFinite(el.duration) ? el.duration : 0, volume: el.volume });
    const events = ['play', 'pause', 'timeupdate', 'durationchange', 'loadedmetadata', 'volumechange', 'emptied'];
    update(); events.forEach((name) => el.addEventListener(name, update));
    return () => events.forEach((name) => el.removeEventListener(name, update));
  }, [audio]);
  return <CompactPlayer {...state} title={title} subtitle={`${t('player.mini.library')} · ${index + 1} / ${count}`} busy={loading}
    previousDisabled={index <= 0} nextDisabled={index >= count - 1}
    onToggle={() => { const el = audio.current; if (el) { if (el.paused) void el.play().catch(onError); else el.pause(); } }}
    onPrevious={onPrevious} onNext={onNext} onSeek={(value) => { if (audio.current) audio.current.currentTime = value; }}
    onVolume={(value) => { if (audio.current) { audio.current.volume = value; audio.current.muted = value === 0; } }}
    onOpen={onOpen} onClose={onClose} />;
}

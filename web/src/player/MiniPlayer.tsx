import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import type { BrowserPlayerStatus, PlayerCommand } from '../api';
import './miniPlayer.css';

export interface MiniPlayerLabels {
  player: string; pause: string; resume: string; louder: string; quieter: string;
  previous: string; next: string; volume: string; expand: string; collapse: string;
  close: string; nowPlaying: string; seek: string; local: string; library: string;
}
export interface PlayerLayout { width: number; height: number }

function clock(seconds: number) {
  const n = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
}

function Glyph({ name }: { name: 'play' | 'pause' | 'previous' | 'next' | 'expand' | 'collapse' | 'close' | 'plus' | 'minus' }) {
  const paths = {
    play: <path d="m9 5 11 7-11 7z" />,
    pause: <path d="M8 5v14M16 5v14" />,
    previous: <path d="M5 5v14m14-14L8 12l11 7z" />,
    next: <path d="M19 5v14M5 5l11 7-11 7z" />,
    expand: <path d="m6 7 5 5-5 5m7-10 5 5-5 5" />,
    collapse: <path d="m18 7-5 5 5 5m-7-10-5 5 5 5" />,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    plus: <path d="M5 12h14M12 5v14" />,
    minus: <path d="M5 12h14" />,
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

/** One player for the web portrait and the desktop portrait, through adapters. */
export default function MiniPlayer({ status, control, labels, onLayout, visible = true, hidden = false, onActivity, onVisibilityChange }: {
  status: () => Promise<BrowserPlayerStatus>;
  control: (command: PlayerCommand, value?: number) => Promise<BrowserPlayerStatus>;
  labels: MiniPlayerLabels;
  onLayout?: (layout: PlayerLayout) => void;
  visible?: boolean;
  /** Hide during avatar movement, including while a control request is pending. */
  hidden?: boolean;
  onActivity?: () => void;
  onVisibilityChange?: (visible: boolean) => void;
}) {
  const [current, setCurrent] = useState<BrowserPlayerStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  const [seek, setSeek] = useState<number | null>(null);
  const [volume, setVolume] = useState<number | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const focusToggle = useRef(false);
  const pending = useRef(false);
  const acting = useRef(false);
  const revision = useRef(0);
  const alive = useRef(true);
  const running = !!current?.running;
  const showing = !hidden && (visible || busy);

  useEffect(() => {
    alive.current = true;
    const poll = async () => {
      if (pending.current || acting.current) return;
      pending.current = true;
      const before = revision.current;
      try {
        const next = await status();
        if (alive.current && before === revision.current) setCurrent(next);
      } catch { /* Keep a retryable close button if the core briefly disconnects. */ }
      finally { pending.current = false; }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 2000);
    return () => { alive.current = false; window.clearInterval(timer); };
  }, [status]);

  useLayoutEffect(() => {
    if (!running) { onLayout?.({ width: 0, height: 0 }); return; }
    // Auto-hide only removes the controls. Keep the native window reservation
    // until playback stops, so hovering cannot resize or reposition the avatar.
    if (!showing) return;
    const element = root.current;
    if (!element || !onLayout) return;
    const report = () => onLayout({ width: expanded ? 208 : window.matchMedia('(pointer: coarse)').matches ? 106 : 58, height: Math.ceil(element.offsetHeight) });
    report();
    const observer = new ResizeObserver(report);
    observer.observe(element);
    return () => observer.disconnect();
  }, [running, showing, expanded, onLayout]);
  useEffect(() => () => onLayout?.({ width: 0, height: 0 }), [onLayout]);
  useEffect(() => { if (!running) setExpanded(false); }, [running]);
  useLayoutEffect(() => { onVisibilityChange?.(running && showing); }, [running, showing, onVisibilityChange]);
  useEffect(() => { if (running) onActivity?.(); }, [running, onActivity]);
  useLayoutEffect(() => {
    if (focusToggle.current) { focusToggle.current = false; root.current?.querySelector<HTMLButtonElement>('[data-player-toggle]')?.focus(); }
  }, [expanded]);

  function toggleExpanded(value: boolean) { focusToggle.current = true; setExpanded(value); }
  async function act(command: PlayerCommand, value?: number) {
    if (acting.current) return;
    acting.current = true;
    revision.current++;
    setBusy(true); setError('');
    try { const next = await control(command, value); if (alive.current) setCurrent(next); }
    catch (e) { if (alive.current) setError(e instanceof Error ? e.message : String(e)); }
    finally {
      revision.current++;
      acting.current = false;
      if (alive.current) { setBusy(false); setSeek(null); setVolume(null); }
      if (alive.current) onActivity?.();
    }
  }

  if (!current?.running || !showing) return null;
  const playing = current.state === 'playing' || current.state === 'buffering';
  const source = current.source === 'local' ? labels.local : current.source === 'library' ? labels.library
    : current.source === 'youtube_music' ? 'YouTube Music' : 'YouTube';
  const shortSource = current.source === 'youtube_music' ? 'YT Music' : current.source === 'local' ? 'MP3'
    : current.source === 'library' ? labels.library : 'YouTube';
  const button = (name: Parameters<typeof Glyph>[0]['name'], label: string, action: () => void, primary = false) =>
    <button type="button" className={`avatar-mini-button${primary ? ' avatar-mini-primary' : ''}`} disabled={busy} title={label} aria-label={label} onClick={action}><Glyph name={name} /></button>;
  const playButton = () => button(playing ? 'pause' : 'play', playing ? labels.pause : labels.resume, () => { void act(playing ? 'pause' : 'resume'); }, true);
  const stop = () => { void act('stop'); };
  const commitSeek = (value: string) => { void act('seek', Number(value)); };
  const commitVolume = (value: string) => { void act('volume', Number(value) / 100); };
  const rangeKeys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];

  return <div ref={root} className={`avatar-mini-player${expanded ? ' avatar-mini-player--expanded' : ''}`} data-solid
    role="group" aria-label={labels.player}
    onPointerDown={(e) => { onActivity?.(); e.stopPropagation(); }} onPointerUp={(e) => e.stopPropagation()} onClick={(e) => { onActivity?.(); e.stopPropagation(); }}
    onFocusCapture={() => onActivity?.()} onInputCapture={() => onActivity?.()}
    onWheel={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()} onContextMenu={(e) => e.stopPropagation()}
    onKeyDown={(e) => { onActivity?.(); e.stopPropagation(); if (e.key === 'Escape' && expanded) toggleExpanded(false); }}>
    {!expanded && <div className="avatar-mini-caption"><strong title={current.title}>{current.title || labels.player}</strong><span title={source}>{shortSource}</span></div>}
    <div className="avatar-mini-surface">
      {!expanded ? <div className="avatar-mini-dock">
        {button('close', labels.close, stop)}
        <button type="button" className="avatar-mini-button" data-player-toggle title={labels.expand} aria-label={labels.expand} aria-expanded={false} onClick={() => toggleExpanded(true)}><Glyph name="expand" /></button>
        {button('previous', labels.previous, () => { void act('previous'); })}
        {button('next', labels.next, () => { void act('next'); })}
        {playButton()}
        <output className="avatar-mini-percent" aria-label={labels.volume}>{Math.round(current.volume * 100)}%</output>
        {button('minus', labels.quieter, () => { void act('volume_down'); })}
        {button('plus', labels.louder, () => { void act('volume_up'); })}
      </div> : <div className="avatar-mini-card">
        <div className="avatar-mini-card-top"><span title={current.playlist_name || labels.nowPlaying}>{current.playlist_name || labels.nowPlaying}{current.playlist_id ? ` · ${(current.queue_index ?? 0) + 1}/${current.queue_length}` : ''}</span><div className="avatar-mini-actions">
          <button type="button" className="avatar-mini-button" data-player-toggle title={labels.collapse} aria-label={labels.collapse} aria-expanded={true} onClick={() => toggleExpanded(false)}><Glyph name="collapse" /></button>
          {button('close', labels.close, stop)}
        </div></div>
        <div className="avatar-mini-track"><div className="avatar-mini-cover" aria-hidden="true"><span>{(current.title || 'VPC').slice(0, 7).toUpperCase()}</span><i /></div>
          <div><strong title={current.title}>{current.title || labels.player}</strong><span>{source}</span></div>
        </div>
        <div className="avatar-mini-timeline">
          <input type="range" min="0" max={Math.max(1, Math.floor(current.duration))} value={seek ?? Math.min(Math.floor(current.position), Math.max(1, Math.floor(current.duration)))}
            style={{ '--mini-progress': `${current.duration > 0 ? 100 * (seek ?? current.position) / current.duration : 0}%` } as CSSProperties}
            disabled={busy || current.duration <= 0} aria-label={labels.seek}
            onChange={(e) => setSeek(Number(e.target.value))} onPointerUp={(e) => commitSeek(e.currentTarget.value)}
            onKeyUp={(e) => { if (rangeKeys.includes(e.key)) commitSeek(e.currentTarget.value); }} />
          <div><span>{clock(seek ?? current.position)}</span><span>{clock(current.duration)}</span></div>
        </div>
        <div className="avatar-mini-transport">{button('previous', labels.previous, () => { void act('previous'); })}{playButton()}{button('next', labels.next, () => { void act('next'); })}</div>
        <div className="avatar-mini-volume">
          {button('minus', labels.quieter, () => { void act('volume_down'); })}
          <input type="range" min="0" max="100" value={volume ?? Math.round(current.volume * 100)} disabled={busy} aria-label={labels.volume}
            style={{ '--mini-progress': `${volume ?? Math.round(current.volume * 100)}%` } as CSSProperties}
            onChange={(e) => setVolume(Number(e.target.value))} onPointerUp={(e) => commitVolume(e.currentTarget.value)}
            onKeyUp={(e) => { if (rangeKeys.includes(e.key)) commitVolume(e.currentTarget.value); }} />
          {button('plus', labels.louder, () => { void act('volume_up'); })}<output>{volume ?? Math.round(current.volume * 100)}%</output>
        </div>
      </div>}
    </div>
    {(error || current.error) && <p className="avatar-mini-error" role="alert">{error || current.error}</p>}
  </div>;
}

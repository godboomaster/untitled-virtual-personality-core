import { useCallback, useEffect, useRef, useState } from 'react';
import { browserPlayerStatus, controlBrowserMusic, getPlayerSettings, isMissingRoute, playBrowserMusic, playSong, setPlayerProvider } from '../api';
import type { BrowserPlayerStatus, MusicProvider } from '../api';
import { useI18n } from '../i18n';
import SavedMusic from './SavedMusic';
import Select from '../components/Select';
import CompactPlayer from './CompactPlayer';

function clock(seconds: number) {
  if (!Number.isFinite(seconds)) return '0:00';
  const n = Math.max(0, Math.floor(seconds));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
}

function saved(key: string, fallback: string) {
  try { return localStorage.getItem(`vpc-player-browser-${key}`) ?? fallback; } catch { return fallback; }
}

export default function BrowserPlayback({ visible, onStart, onRunning, onOpen }: {
  visible: boolean; onStart: () => void; onRunning: (running: boolean) => void; onOpen: () => void;
}) {
  const { t } = useI18n();
  const [url, setUrl] = useState(() => saved('url', ''));
  const [headless, setHeadless] = useState(() => saved('headless', 'true') !== 'false');
  const [volume, setVolume] = useState(() => {
    const n = Number(saved('volume', '50'));
    return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 50;
  });
  const [status, setStatus] = useState<BrowserPlayerStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [provider, setProvider] = useState<MusicProvider>('youtube_music');
  const [settingsReady, setSettingsReady] = useState(false);
  const [song, setSong] = useState('');
  const pending = useRef(false);
  const revision = useRef(0);
  const onStartRef = useRef(onStart);
  onStartRef.current = onStart;
  const reportedTrack = useRef('');
  const running = !!status?.running && status.source !== 'library';
  const active = status?.state === 'playing' || status?.state === 'buffering';

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const load = () => { void getPlayerSettings().then((next) => {
      if (alive) { setProvider(next.provider); setSettingsReady(true); window.clearInterval(timer); }
    }).catch(() => { /* core may be starting; retry while this page is visible */ }); };
    load();
    if (visible) timer = window.setInterval(load, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [visible]);

  async function changeProvider(next: MusicProvider) {
    setBusy(true);
    setError('');
    try { setProvider((await setPlayerProvider(next)).provider); }
    catch (e) { setError(isMissingRoute(e) ? t('common.coreOutdated') : e instanceof Error ? e.message : t('player.browser.failed')); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    try {
      for (const [key, value] of Object.entries({ url, headless, volume })) localStorage.setItem(`vpc-player-browser-${key}`, String(value));
    } catch { /* playback works without preferences */ }
  }, [url, headless, volume]);

  const received = useCallback((next: BrowserPlayerStatus) => {
    const key = next.state === 'playing' && next.source !== 'library' ? next.url : '';
    if (key && key !== reportedTrack.current) onStartRef.current();
    reportedTrack.current = key;
    setStatus(next);
    onRunning(next.running && next.source !== 'library');
    if (next.running && next.source !== 'library') setVolume(Math.round(next.volume * 100));
  }, [onRunning]);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      if (pending.current) return;
      pending.current = true;
      const before = revision.current;
      try {
        const next = await browserPlayerStatus();
        if (alive && before === revision.current) {
          received(next);
        }
      } catch { /* backend may be offline/older; report errors on explicit actions */ }
      finally { pending.current = false; }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, visible || running ? 2500 : 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [visible, running, received]);

  async function act(action: () => Promise<BrowserPlayerStatus>) {
    if (busy) return;
    revision.current++;
    setBusy(true);
    setError('');
    try { received(await action()); }
    catch (e) { setError(isMissingRoute(e) ? t('common.coreOutdated') : e instanceof Error ? e.message : t('player.browser.failed')); }
    finally { revision.current++; setBusy(false); }
  }

  function start(showWindow = false) {
    const target = showWindow && status?.url ? status.url : url.trim();
    if (!target) return;
    onStart();
    void act(() => playBrowserMusic(target, showWindow ? false : headless, volume / 100));
  }

  const subtitle = status ? [status.source === 'local' ? t('player.mini.local') : status.source === 'youtube_music' ? 'YouTube Music' : 'YouTube',
    status.playlist_id ? `${status.playlist_name || t('player.saved.all')} · ${(status.queue_index ?? 0) + 1}/${status.queue_length}` : '',
    !active ? t(`player.browser.state.${status.state}`) : ''].filter(Boolean).join(' · ') : '';

  return <div className={`music-browser${!visible ? ' music-browser--compact' : ''}`} hidden={!visible && !running && !error}>
    <div hidden={!visible}>
      <h2>{t('player.browser.title')}</h2>
      <p>{t('player.browser.help')}</p>
      <form onSubmit={(e) => {
        e.preventDefault();
        if (busy || !settingsReady || !song.trim()) return;
        onStart(); void act(() => playSong(song.trim(), provider, headless, volume / 100));
      }}>
        <label htmlFor="music-provider">{t('player.browser.provider')}</label>
        <Select id="music-provider" value={provider} disabled={busy || !settingsReady}
          options={[{ value: 'youtube', label: 'YouTube' }, { value: 'youtube_music', label: 'YouTube Music' }]}
          onChange={(value) => { void changeProvider(value as MusicProvider); }} />
        <p>{t('player.browser.searchHelp')}</p>
        <label htmlFor="music-song">{t('player.browser.song')}</label>
        <div className="music-path-row">
          <input id="music-song" value={song} onChange={(e) => setSong(e.target.value)} placeholder={t('player.browser.songPlaceholder')} />
          <button className="btn" disabled={busy || !settingsReady || !song.trim()}>{t('player.browser.findPlay')}</button>
        </div>
      </form>
      <form onSubmit={(e) => { e.preventDefault(); start(); }}>
        <label htmlFor="music-browser-url">{t('player.browser.url')}</label>
        <div className="music-path-row">
          <input id="music-browser-url" type="url" value={url} placeholder="https://music.youtube.com/watch?v=…" onChange={(e) => setUrl(e.target.value)} />
          <button className="btn" disabled={busy || !url.trim()}>{t(busy ? 'player.loading' : 'player.browser.open')}</button>
        </div>
        <label className="music-browser-mode"><input type="checkbox" checked={headless} onChange={(e) => setHeadless(e.target.checked)} />{t('player.browser.headless')}</label>
      </form>
    </div>
    {error && <p className="music-error" role="alert">{error}</p>}
    {!visible && running && status && <CompactPlayer title={status.title || t('player.browser.title')} subtitle={subtitle}
      playing={active} busy={busy} position={status.position} duration={status.duration} volume={volume / 100}
      onToggle={() => { if (!active) onStart(); void act(() => controlBrowserMusic(active ? 'pause' : 'resume')); }}
      onPrevious={() => { void act(() => controlBrowserMusic('previous')); }} onNext={() => { void act(() => controlBrowserMusic('next')); }}
      onSeek={(value) => { setStatus({ ...status, position: value }); return act(() => controlBrowserMusic('seek', value)); }}
      onVolume={(value) => { setVolume(Math.round(value * 100)); return act(() => controlBrowserMusic('volume', value)); }}
      onOpen={onOpen} onClose={() => { void act(() => controlBrowserMusic('stop')); }} />}
    {visible && status?.running && status.source !== 'library' && <div className="music-browser-status">
      <strong>{status.title || t('player.browser.title')}</strong>
      <p role="status">{t(`player.browser.state.${status.state}`)} · {clock(status.position)}{status.duration > 0 ? ` / ${clock(status.duration)}` : ''} · {t(status.headless ? 'player.browser.hidden' : 'player.browser.window')}</p>
      {(status.state === 'waiting' || status.state === 'error') && <p>{status.error || t('player.browser.waitingHelp')}</p>}
      {status.save_error && <p role="alert">{t('player.saved.autoFailed')} {status.save_error}</p>}
      {status.playlist_id && <p>{status.playlist_name || t('player.saved.all')} · {(status.queue_index ?? 0) + 1} / {status.queue_length}</p>}
      <div className="music-controls">
        <button className="btn" disabled={busy} onClick={() => {
          if (!active) onStart();
          void act(() => controlBrowserMusic(active ? 'pause' : 'resume'));
        }}>{t(active ? 'player.browser.pause' : 'player.browser.resume')}</button>
        <button className="btn" disabled={busy} onClick={() => { void act(() => controlBrowserMusic('previous')); }}>{t('player.previous')}</button>
        <button className="btn" disabled={busy} onClick={() => { void act(() => controlBrowserMusic('next')); }}>{t('player.next')}</button>
        <button className="btn" disabled={busy} onClick={() => { void act(() => controlBrowserMusic('stop')); }}>{t('player.browser.stop')}</button>
        {status.headless && status.source !== 'local' && status.url && <button className="btn" disabled={busy} onClick={() => start(true)}>{t('player.browser.show')}</button>}
      </div>
      {status.duration > 0 && <label className="music-browser-slider">{t('player.browser.seek')}
        <input type="range" min="0" max={Math.floor(status.duration)} value={Math.floor(status.position)} disabled={busy}
          onChange={(e) => { const value = Number(e.target.value); setStatus({ ...status, position: value }); }}
          onPointerUp={(e) => { void act(() => controlBrowserMusic('seek', Number(e.currentTarget.value))); }}
          onKeyUp={(e) => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(e.key)) void act(() => controlBrowserMusic('seek', Number(e.currentTarget.value))); }} />
      </label>}
    </div>}
    {visible && <label className="music-browser-slider">{t('player.browser.volume')} · {volume}%
      <input type="range" min="0" max="100" value={volume} disabled={busy}
        onChange={(e) => setVolume(Number(e.target.value))}
        onPointerUp={(e) => { if (running) void act(() => controlBrowserMusic('volume', Number(e.currentTarget.value) / 100)); }}
        onKeyUp={(e) => { if (running && ['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(e.key)) void act(() => controlBrowserMusic('volume', Number(e.currentTarget.value) / 100)); }} />
    </label>}
    <SavedMusic visible={visible} current={status} headless={headless} volume={volume / 100} playbackBusy={busy}
      play={(action) => { onStart(); void act(action); }} />
  </div>;
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { importPlayerPath, isMissingRoute } from '../api';
import BrowserPlayback from './BrowserPlayback';
import { useLibraryPlayer } from './useLibraryPlayer';
import { useI18n } from '../i18n';
import Icon from '../components/icons';
import { AUDIO_ACCEPT, audioFileError, deleteTrack, listTracks, readTrack, saveTrack } from './library';
import type { Track } from './library';
import { LocalCompactPlayer } from './CompactPlayer';
import './player.css';

function preference(key: string): string {
  try { return localStorage.getItem(`vpc-player-${key}`) ?? ''; } catch { return ''; }
}
function remember(key: string, value: string) {
  try { localStorage.setItem(`vpc-player-${key}`, value); } catch { /* playback still works */ }
}

/** Mounted once in App: changing sections never recreates the audio element. */
export default function Player({ visible, onOpen }: { visible: boolean; onOpen: () => void }) {
  const { t } = useI18n();
  const [tracks, setTracks] = useState<Track[]>([]);
  const [selected, setSelected] = useState(() => preference('selected'));
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [path, setPath] = useState('');
  const [repeat, setRepeat] = useState(() => preference('repeat') === 'true');
  const [playing, setPlaying] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [browserRunning, setBrowserRunning] = useState(false);
  const [localActive, setLocalActive] = useState(false);
  const remoteWasRunning = useRef(false);
  const audio = useRef<HTMLAudioElement>(null);
  const files = useRef<HTMLInputElement>(null);
  const autoPlay = useRef(false);
  const current = tracks.find((track) => track.id === selected);
  const selectedId = current?.id;
  const failPlayback = useRef(() => {});
  failPlayback.current = () => setError(t('player.error.play'));

  useEffect(() => {
    let alive = true;
    void listTracks().then((rows) => {
      if (!alive) return;
      setTracks(rows);
      setSelected((id) => rows.some((row) => row.id === id) ? id : rows[0]?.id ?? '');
      setReady(true);
    }).catch(() => { if (alive) setError(t('player.error.storage')); });
    return () => { alive = false; };
  // The library belongs to this browser, independent of the selected UI language.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const el = audio.current;
    if (!el) return;
    let alive = true;
    let objectUrl = '';
    el.pause();
    el.removeAttribute('src');
    el.load();
    if (!selectedId) return;
    setLoading(true);
    setError('');
    remember('selected', selectedId);
    void readTrack(selectedId).then(async (blob) => {
      if (!alive) return;
      objectUrl = URL.createObjectURL(blob);
      el.src = objectUrl;
      setLoading(false);
      if (autoPlay.current) {
        autoPlay.current = false;
        try { await el.play(); } catch (e) {
          if (alive && !(e instanceof DOMException && e.name === 'AbortError')) failPlayback.current();
        }
      }
    }).catch(() => {
      if (alive) { setLoading(false); failPlayback.current(); }
    });
    return () => {
      alive = false;
      el.pause();
      el.removeAttribute('src');
      el.load();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [selectedId]);

  useEffect(() => {
    const el = audio.current;
    const saved = Number(preference('volume'));
    if (el && preference('volume') && Number.isFinite(saved)) el.volume = Math.max(0, Math.min(1, saved));
  }, []);

  function choose(id: string) {
    setError('');
    if (id === selected) {
      if (audio.current?.src) void audio.current.play().catch(() => failPlayback.current());
      else autoPlay.current = true;
    } else {
      autoPlay.current = true;
      setSelected(id);
    }
  }

  function move(delta: number) {
    const index = tracks.findIndex((track) => track.id === selected);
    const next = tracks[index + delta];
    if (next) choose(next.id);
  }

  const closeLocal = useCallback(() => {
    autoPlay.current = false;
    setPlaying(false);
    setLocalActive(false);
    setSelected('');
  }, []);

  const libraryPlayer = useLibraryPlayer(audio, current?.name ?? '', async (delta) => {
    const target = tracks[tracks.findIndex((track) => track.id === selected) + delta];
    if (!target) throw new Error(t(delta > 0 ? 'player.lastTrack' : 'player.firstTrack'));
    await new Promise<void>((resolve, reject) => {
      const el = audio.current!;
      const cleanup = () => { clearTimeout(timeout); el.removeEventListener('playing', done); el.removeEventListener('error', failed); };
      const done = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error(t('player.error.play'))); };
      const timeout = setTimeout(failed, 4000);
      el.addEventListener('playing', done); el.addEventListener('error', failed);
      choose(target.id);
    });
  }, closeLocal);
  const deactivateLibrary = libraryPlayer.deactivate;
  const receiveBrowserRunning = useCallback((running: boolean) => {
    setBrowserRunning(running);
    if (running && !remoteWasRunning.current) { deactivateLibrary(); closeLocal(); }
    remoteWasRunning.current = running;
  }, [deactivateLibrary, closeLocal]);

  useEffect(() => {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.metadata = current ? new MediaMetadata({ title: current.name, artist: 'Virtual Persona Core' }) : null;
    navigator.mediaSession.playbackState = current ? (playing ? 'playing' : 'paused') : 'none';
    const handlers: [MediaSessionAction, MediaSessionActionHandler][] = [
      ['play', () => { void audio.current?.play().catch(() => failPlayback.current()); }],
      ['pause', () => audio.current?.pause()],
      ['previoustrack', () => move(-1)], ['nexttrack', () => move(1)],
      ['seekto', (details) => { if (audio.current && details.seekTime != null) audio.current.currentTime = details.seekTime; }],
    ];
    for (const [action, handler] of handlers) {
      try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* platform may lack the action */ }
    }
    return () => {
      for (const [action] of handlers) {
        try { navigator.mediaSession.setActionHandler(action, null); } catch { /* unsupported */ }
      }
    };
  });

  async function addFiles(incoming: File[]) {
    if (busy || !ready) return;
    setBusy(true);
    setError('');
    setNotice('');
    const problems: string[] = [];
    let added = 0;
    try {
      for (const file of incoming) {
        const issue = audioFileError(file);
        if (issue) { problems.push(`${file.name}: ${t(`player.error.${issue}`)}`); continue; }
        await saveTrack(file);
        added++;
      }
    } catch { problems.push(t('player.error.storage')); }
    try {
      const rows = await listTracks();
      setTracks(rows);
      setSelected((id) => id || rows[0]?.id || '');
    } catch { problems.push(t('player.error.storage')); }
    setError(problems.join('\n'));
    if (added) setNotice(t('player.added', { n: added }));
    setBusy(false);
  }

  async function addPath() {
    if (busy || !path.trim() || !ready) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const blob = await importPlayerPath(path.trim());
      const name = path.trim().replace(/^"|"$/g, '').split(/[\\/]/).pop() || 'track.mp3';
      // Stable timestamp deduplicates repeated imports of the same path/size.
      const file = new File([blob], name, { type: blob.type, lastModified: 0 });
      const issue = audioFileError(file);
      if (issue) throw new Error(t(`player.error.${issue}`));
      const track = await saveTrack(file);
      const rows = await listTracks();
      setTracks(rows);
      setSelected((id) => id || track.id);
      setPath('');
      setNotice(t('player.added', { n: 1 }));
    } catch (e) {
      setError(isMissingRoute(e) ? t('common.coreOutdated')
        : e instanceof DOMException ? t('player.error.storage')
        : e instanceof Error ? e.message : t('player.error.import'));
    }
    setBusy(false);
  }

  async function remove(track: Track) {
    try {
      await deleteTrack(track.id);
      const index = tracks.findIndex((row) => row.id === track.id);
      const rows = tracks.filter((row) => row.id !== track.id);
      if (track.id === selected) {
        autoPlay.current = false;
        setSelected(rows[index]?.id ?? rows[index - 1]?.id ?? '');
      }
      setTracks(rows);
      setError('');
    } catch { setError(t('player.error.storage')); }
  }

  const index = tracks.findIndex((track) => track.id === selected);
  return (
    <div className={visible ? 'section music-player' : 'music-player music-player--mini'} hidden={!visible && !localActive && !browserRunning}>
      <div hidden={!visible}>
        <div className="section-header">
          <div><h1 className="section-title">{t('nav.player')}</h1><p className="section-subtitle">{t('player.subtitle')}</p></div>
        </div>
        <div className={`music-import${dragging ? ' music-import--drag' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false); }}
          onDrop={(e) => { e.preventDefault(); setDragging(false); void addFiles(Array.from(e.dataTransfer.files)); }}>
          <Icon name="disc" size={30} />
          <strong>{t('player.drop')}</strong>
          <p>{t('player.formats')}</p>
          <button className="btn" disabled={busy || !ready} onClick={() => files.current?.click()}>{t(busy ? 'player.importing' : 'player.choose')}</button>
          <input ref={files} type="file" accept={AUDIO_ACCEPT} multiple hidden onChange={(e) => {
            void addFiles(Array.from(e.currentTarget.files ?? [])); e.currentTarget.value = '';
          }} />
        </div>
        <form className="music-path" onSubmit={(e) => { e.preventDefault(); void addPath(); }}>
          <label htmlFor="music-path">{t('player.path')}</label>
          <div className="music-path-row">
            <input id="music-path" className="input" value={path} onChange={(e) => setPath(e.target.value)} placeholder={t('player.pathPlaceholder')} autoComplete="off" />
            <button className="btn" disabled={busy || !ready || !path.trim()}>{t('common.add')}</button>
          </div>
          <p>{t('player.pathHelp')}</p>
        </form>
      </div>
      {error && <p className="music-error" role="alert">{error}</p>}
      <div className="music-now" hidden={!current || (!visible && (!localActive || browserRunning))}>
        <div className="music-now-title" hidden={!visible}><Icon name="disc" size={20} /><strong>{current?.name}</strong></div>
        {loading && visible && <span role="status">{t('player.loading')}</span>}
        <audio ref={audio} controls hidden={!visible} preload="metadata" loop={repeat}
          aria-label={current?.name ?? t('nav.player')}
          onPlay={() => {
            setPlaying(true);
            setLocalActive(true);
            libraryPlayer.activate();
          }} onPause={() => setPlaying(false)}
          onEnded={() => { libraryPlayer.finish(); closeLocal(); }}
          onVolumeChange={() => { if (audio.current) remember('volume', String(audio.current.volume)); }}
          onError={() => { if (audio.current?.getAttribute('src')) failPlayback.current(); }} />
        <div className="music-controls" hidden={!visible}>
          <button className="btn" disabled={index <= 0 || loading} onClick={() => move(-1)}>{t('player.previous')}</button>
          <button className="btn" disabled={index < 0 || index >= tracks.length - 1 || loading} onClick={() => move(1)}>{t('player.next')}</button>
          <button className="btn" aria-pressed={repeat} onClick={() => { setRepeat(!repeat); remember('repeat', String(!repeat)); }}>{t('player.repeat')}</button>
          <span>{index + 1} / {tracks.length}</span>
        </div>
        {!visible && current && localActive && !browserRunning && <LocalCompactPlayer audio={audio} title={current.name} index={index} count={tracks.length} loading={loading}
          onPrevious={() => move(-1)} onNext={() => move(1)} onOpen={onOpen} onError={() => failPlayback.current()}
          onClose={() => { audio.current?.pause(); libraryPlayer.finish(); closeLocal(); }} />}
      </div>
      <BrowserPlayback visible={visible} onRunning={receiveBrowserRunning} onOpen={onOpen} onStart={() => {
        autoPlay.current = false;
        audio.current?.pause();
      }} />
      <div hidden={!visible}>
        {notice && <p role="status">{notice}</p>}
        <div className="music-library-heading"><h2>{t('player.library')}</h2><span>{tracks.length}</span></div>
        {!tracks.length && <p className="music-empty">{t(ready ? 'player.empty' : 'player.loading')}</p>}
        <ol className="music-library">
          {tracks.map((track, i) => <li key={track.id} className={track.id === selected ? 'music-track music-track--active' : 'music-track'}>
            <button className="music-track-play" onClick={() => choose(track.id)} aria-current={track.id === selected ? 'true' : undefined}>
              <span className="music-track-index">{track.id === selected && playing ? '▶' : i + 1}</span>
              <span className="music-track-name">{track.name}</span>
              <span className="music-track-size">{(track.size / 1024 / 1024).toFixed(1)} {t('player.mb')}</span>
            </button>
            <button className="btn btn--icon" disabled={busy} aria-label={t('player.remove', { name: track.name })} onClick={() => { void remove(track); }}><Icon name="trash" size={16} /></button>
          </li>)}
        </ol>
        <p className="music-storage-note">{t('player.storage')}</p>
      </div>
    </div>
  );
}

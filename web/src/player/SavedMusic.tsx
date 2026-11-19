import { useCallback, useEffect, useRef, useState } from 'react';
import { createMusicPlaylist, deleteMusicPlaylist, deleteSongLink, getMusicCatalog, isMissingRoute, playBrowserMusic, playMusicPlaylist, saveSongLink, updateMusicPlaylist } from '../api';
import type { BrowserPlayerStatus, MusicCatalog } from '../api';
import { useI18n } from '../i18n';
import Select from '../components/Select';

export default function SavedMusic({ visible, current, headless, volume, playbackBusy, play }: {
  visible: boolean; current: BrowserPlayerStatus | null; headless: boolean; volume: number; playbackBusy: boolean;
  play: (action: () => Promise<BrowserPlayerStatus>) => void;
}) {
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<MusicCatalog | null>(null);
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [rename, setRename] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [url, setUrl] = useState('');
  const [title, setTitle] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmDelete, setConfirmDelete] = useState('');
  const revision = useRef(-1);
  const mutating = useRef(false);
  const receive = useCallback((next: MusicCatalog) => {
    if (next.revision < revision.current) return;
    revision.current = next.revision;
    setCatalog(next);
    setSelected((id) => id && !next.playlists.some((p) => p.id === id) ? '' : id);
  }, []);
  useEffect(() => {
    if (!visible) return;
    let alive = true;
    let pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try { const next = await getMusicCatalog(); if (alive) { receive(next); setLoadError(''); } }
      catch (e) { if (alive) setLoadError(isMissingRoute(e) ? t('common.coreOutdated') : String(e instanceof Error ? e.message : e)); }
      finally { pending = false; }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [visible, current?.url, receive, t]);

  const playlist = catalog?.playlists.find((p) => p.id === selected);
  const tracks = playlist ? playlist.track_ids.map((id) => catalog?.tracks.find((track) => track.id === id)).filter((row) => !!row) : catalog?.tracks ?? [];
  const disabled = busy || playbackBusy;
  async function mutate(action: () => Promise<MusicCatalog>) {
    if (mutating.current) return null;
    mutating.current = true;
    setBusy(true); setError(''); setNotice(''); setConfirmDelete('');
    try { const next = await action(); receive(next); return next; }
    catch (e) { setError(isMissingRoute(e) ? t('common.coreOutdated') : e instanceof Error ? e.message : String(e)); return null; }
    finally { mutating.current = false; setBusy(false); }
  }
  async function addLink(link: string, songTitle: string) {
    const next = await mutate(() => saveSongLink(link, songTitle, selected || undefined));
    if (next) { setUrl(''); setTitle(''); setNotice(t('player.saved.saved')); }
  }
  function move(index: number, delta: number) {
    if (!playlist) return;
    const ids = [...playlist.track_ids];
    [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
    void mutate(() => updateMusicPlaylist(playlist.id, { track_ids: ids }));
  }
  const currentLink = current?.running && (current.source === 'youtube' || current.source === 'youtube_music') && /[?&]v=/.test(current.url) ? current : null;

  return <section className="music-saved" hidden={!visible} aria-label={t('player.saved.title')}>
    <h2>{t('player.saved.title')}</h2><p>{t('player.saved.help')}</p>
    <div className="music-saved-selector">
      <label htmlFor="music-playlist">{t('player.saved.playlist')}</label>
      <Select id="music-playlist" value={selected} disabled={!catalog || disabled}
        options={[{ value: '', label: `${t('player.saved.all')} · ${catalog?.tracks.length ?? 0}` },
          ...(catalog?.playlists.map((p) => ({ value: p.id, label: `${p.name} · ${p.track_ids.length}` })) ?? [])]}
        onChange={(value) => { setSelected(value); setRenaming(false); setConfirmDelete(''); setNotice(''); }} />
      <button type="button" className="btn" disabled={disabled || !tracks.length} onClick={() => play(() => playMusicPlaylist(selected || undefined, undefined, headless, volume))}>{t('player.saved.play')}</button>
    </div>
    <form className="music-saved-create" onSubmit={(e) => { e.preventDefault(); if (!name.trim() || disabled) return;
      void mutate(() => createMusicPlaylist(name.trim())).then((next) => { if (next?.playlist_id) { setSelected(next.playlist_id); setName(''); } });
    }}>
      <label htmlFor="music-playlist-name">{t('player.saved.newPlaylist')}</label>
      <div className="music-path-row"><input id="music-playlist-name" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} placeholder={t('player.saved.namePlaceholder')} />
        <button className="btn" disabled={disabled || !catalog || !name.trim()}>{t('player.saved.create')}</button></div>
    </form>
    {playlist && <div className="music-saved-manage">
      {!renaming ? <button type="button" className="btn" disabled={disabled} onClick={() => { setRename(playlist.name); setRenaming(true); }}>{t('player.saved.rename')}</button>
        : <form className="music-path-row" onSubmit={(e) => { e.preventDefault(); if (!rename.trim() || disabled) return;
          void mutate(() => updateMusicPlaylist(playlist.id, { name: rename.trim() })).then((next) => { if (next) setRenaming(false); });
        }}><input aria-label={t('player.saved.newPlaylist')} value={rename} maxLength={100} onChange={(e) => setRename(e.target.value)} /><button className="btn" disabled={disabled || !rename.trim()}>{t('common.save')}</button><button type="button" className="btn" onClick={() => setRenaming(false)}>{t('common.cancel')}</button></form>}
      {confirmDelete === 'playlist' ? <><span>{t('player.saved.confirmPlaylist')}</span><button type="button" className="btn" disabled={disabled} onClick={() => { void mutate(() => deleteMusicPlaylist(playlist.id)); }}>{t('common.delete')}</button><button type="button" className="btn" onClick={() => setConfirmDelete('')}>{t('common.cancel')}</button></>
        : <button type="button" className="btn" disabled={disabled} onClick={() => setConfirmDelete('playlist')}>{t('player.saved.deletePlaylist')}</button>}
    </div>}
    <form className="music-saved-add" onSubmit={(e) => { e.preventDefault(); if (url.trim() && !disabled) void addLink(url.trim(), title.trim()); }}
      onDragOver={(e) => { if (!disabled && !e.dataTransfer.types.includes('Files')) e.preventDefault(); }}
      onDrop={(e) => {
        if (disabled || e.dataTransfer.files.length) return;
        const link = (e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain')).split('\n').find((line) => line.trim() && !line.startsWith('#'))?.trim();
        if (link) { e.preventDefault(); void addLink(link, ''); }
      }}>
      <label htmlFor="music-saved-url">{t('player.saved.addLink')}</label>
      <input id="music-saved-url" type="url" value={url} maxLength={2048} onChange={(e) => setUrl(e.target.value)} placeholder="https://music.youtube.com/watch?v=…" required />
      <input aria-label={t('player.saved.songTitle')} value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} placeholder={t('player.saved.songTitle')} />
      <div className="music-controls"><button className="btn" disabled={disabled || !catalog || !url.trim()}>{t(selected ? 'player.saved.addToPlaylist' : 'player.saved.saveLink')}</button>
        {currentLink && <button type="button" className="btn" disabled={disabled || !catalog} onClick={() => { void addLink(currentLink.url, currentLink.title.replace(/\s*[-–|]\s*YouTube(?: Music)?$/, '')); }}>{t('player.saved.saveCurrent')}</button>}
      </div>
    </form>
    {(error || loadError) && <p className="music-error" role="alert">{error || loadError}</p>}
    {notice && <p role="status">{notice}</p>}
    {!tracks.length && catalog && <p>{t(selected ? 'player.saved.emptyPlaylist' : 'player.saved.empty')}</p>}
    <ol className="music-saved-list">
      {tracks.map((track, index) => <li key={track.id}>
        <div className="music-saved-song">
          <button type="button" className="music-saved-play" disabled={disabled} title={t('player.saved.playSong', { name: track.title })}
            onClick={() => play(() => selected ? playMusicPlaylist(selected, track.id, headless, volume) : playBrowserMusic(track.url, headless, volume))}><span>{index + 1}</span><strong>{track.title}</strong></button>
          <a href={track.url} target="_blank" rel="noreferrer">{track.source === 'youtube_music' ? 'YouTube Music' : 'YouTube'}</a>
        </div>
        <div className="music-saved-song-actions">
          {catalog && catalog.playlists.some((p) => !p.track_ids.includes(track.id)) && <Select ariaLabel={t('player.saved.addNamed', { name: track.title })} value="" disabled={disabled}
            options={[{ value: '', label: t('player.saved.addToOther') }, ...catalog.playlists.filter((p) => !p.track_ids.includes(track.id)).map((p) => ({ value: p.id, label: p.name }))]}
            onChange={(id) => { if (id) void mutate(() => updateMusicPlaylist(id, { add_track_id: track.id })).then((next) => { if (next) setNotice(t('player.saved.saved')); }); }} />}
          {playlist && <>
            <button type="button" className="btn" disabled={disabled || index === 0} aria-label={t('player.saved.up', { name: track.title })} onClick={() => move(index, -1)}>↑</button>
            <button type="button" className="btn" disabled={disabled || index === tracks.length - 1} aria-label={t('player.saved.down', { name: track.title })} onClick={() => move(index, 1)}>↓</button>
            <button type="button" className="btn" disabled={disabled} onClick={() => { void mutate(() => updateMusicPlaylist(playlist.id, { remove_track_id: track.id })); }}>{t('player.saved.removeFromPlaylist')}</button>
          </>}
          {!playlist && (confirmDelete === track.id ? <><span>{t('player.saved.confirmSong')}</span><button type="button" className="btn" disabled={disabled} onClick={() => { void mutate(() => deleteSongLink(track.id)); }}>{t('common.delete')}</button><button type="button" className="btn" onClick={() => setConfirmDelete('')}>{t('common.cancel')}</button></>
            : <button type="button" className="btn" disabled={disabled} onClick={() => setConfirmDelete(track.id)}>{t('player.saved.deleteSong')}</button>)}
        </div>
      </li>)}
    </ol>
  </section>;
}

"""Opt-in real Chrome regression using temporary profiles and a temporary catalog.

PYTHONPATH=. python -m scripts.test_music_playlists_live --last youtube_music
Use --last youtube to check a playlist ending on regular YouTube instead.
Seeks near track endings; sound is quiet. No personal profile or library is used.
"""
import argparse
import json, tempfile, time
from pathlib import Path
from unittest.mock import patch
from app.features import music_browser as mb, music_catalog as cat

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--last', choices=['youtube', 'youtube_music'], default='youtube_music')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='vpc-playlists-live-') as folder, patch.object(mb, 'data_dir', return_value=Path(folder)), patch.object(cat, 'data_dir', return_value=Path(folder)):
        b = mb.MusicBrowser()
        report = []

        def status():
            return b.execute_sync('status')

        def wait_playing():
            end = time.monotonic() + 30
            while time.monotonic() < end:
                s = status()
                if s['state'] == 'error':
                    raise AssertionError(s)
                if s['state'] == 'playing' and (not s.get('ad')) and (s['duration'] > 20):
                    return s
                time.sleep(0.5)
            raise AssertionError(status())
        try:
            s = b.execute_sync('search', query='Rick Astley Never Gonna Give You Up', provider='youtube', volume=0.02)
            s = wait_playing()
            data = cat.snapshot()
            assert len(data['tracks']) == 1, data
            assert 'Rick' in data['tracks'][0]['title'] or 'Never Gonna' in data['tracks'][0]['title'], data
            print('PASS search automatically saved', data['tracks'][0]['title'], flush=True)
            key = cat.create_playlist('Live playlist')['playlist_id']
            two = cat.save_song('https://music.youtube.com/watch?v=0WA2NX4kPSI', playlist_id=key)['saved_track_id']
            one = data['tracks'][0]['id']
            cat.update_playlist(key, add_track_id=one)
            if args.last == 'youtube_music':
                cat.update_playlist(key, track_ids=[one, two])
            first_source = 'youtube_music' if args.last == 'youtube' else 'youtube'
            s = b.execute_sync('playlist', playlist_id=key, volume=0.02)
            s = wait_playing()
            assert s['queue_index'] == 0 and s['queue_length'] == 2 and (s['source'] == first_source), s
            report.append(s)
            print('PASS start mixed-service playlist', flush=True)
            b.execute_sync('control', command='next')
            s = wait_playing()
            assert s['queue_index'] == 1 and s['source'] == args.last, s
            b.execute_sync('control', command='previous')
            s = wait_playing()
            assert s['queue_index'] == 0 and s['source'] == first_source, s
            paused = b.execute_sync('control', command='pause')
            time.sleep(1.3)
            assert abs(status()['position'] - paused['position']) < 0.2
            b.execute_sync('control', command='resume')
            s = wait_playing()
            print('PASS next/previous/pause/resume', flush=True)
            b.execute_sync('control', command='seek', value=s['duration'] - 1.5)
            end = time.monotonic() + 30
            while time.monotonic() < end:
                s = status()
                if s['queue_index'] == 1 and s['state'] == 'playing':
                    break
                time.sleep(0.5)
            else:
                raise AssertionError(('auto advance failed', s, b._playlist))
            assert s['source'] == args.last, s
            print('PASS automatic advance without a UI', flush=True)
            report.append(s)
            s = wait_playing()
            b.execute_sync('control', command='seek', value=s['duration'] - 1.5)
            end = time.monotonic() + 30
            while time.monotonic() < end:
                s = status()
                if not s['running']:
                    break
                time.sleep(0.5)
            else:
                raise AssertionError(('last track did not finish', s))
            time.sleep(3)
            s = status()
            assert not s['running'] and b._playlist is None and b._context is None, s
            report.append(s)
            print('PASS last song fully closes the player', flush=True)
            b.execute_sync('stop')
            assert not status()['running'] and b._playlist is None
            assert len(cat.snapshot()['tracks']) == 2
            print('PASS saved on disk, stop clears queue, no duplicate tracks', flush=True)
        finally:
            b.execute_sync('stop')
            b._worker.shutdown(wait=False)
            Path('data/player/benchmarks').mkdir(parents=True, exist_ok=True)
            Path('data/player/benchmarks/playlists-live.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
if __name__ == '__main__':
    main()

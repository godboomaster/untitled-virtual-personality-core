"""Song catalog persistence, playlist API, auto-save and queue behavior. No Chrome."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from app.api.player_api import create_router
from app.features import music_catalog as catalog, music_player as mp
from app.features.music_browser import MusicBrowser, MusicBrowserError

FIRST = 'https://www.youtube.com/watch?v=0WA2NX4kPSI'
SECOND = 'https://music.youtube.com/watch?v=dQw4w9WgXcQ'


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(catalog, 'data_dir', return_value=self.root)
        p.start(); self.addCleanup(p.stop)

    def playlist(self):
        result = catalog.create_playlist('Для работы')
        return result['playlist_id']


class CatalogTests(CatalogFixture):
    def test_same_video_deduplicates_and_explicit_name_survives_auto_save(self):
        catalog.save_song(FIRST+'&t=15&list=PL123456', 'Моё название', custom_title=True)
        result = catalog.save_song('https://music.youtube.com/watch?v=0WA2NX4kPSI&feature=share', 'Actual title', origin='search')
        self.assertEqual(len(result['tracks']), 1)
        row = result['tracks'][0]
        self.assertEqual(row['title'], 'Моё название')
        self.assertEqual(row['url'], 'https://music.youtube.com/watch?v=0WA2NX4kPSI')
        self.assertEqual(catalog.snapshot()['tracks'], result['tracks'])

    def test_non_song_and_unsafe_links_do_not_change_library(self):
        for url in ('https://www.youtube.com/playlist?list=PL123456', 'file:///tmp/song.mp3',
                    'https://youtube.com.evil.test/watch?v=0WA2NX4kPSI', 'https://localhost/watch?v=0WA2NX4kPSI'):
            with self.assertRaises(MusicBrowserError): catalog.save_song(url)
        self.assertEqual(catalog.snapshot()['tracks'], [])

    def test_playlist_order_membership_rename_and_delete(self):
        key = self.playlist()
        one = catalog.save_song(FIRST, 'First', playlist_id=key)['saved_track_id']
        two = catalog.save_song(SECOND, 'Second')['saved_track_id']
        catalog.update_playlist(key, add_track_id=two)
        catalog.update_playlist(key, add_track_id=two)
        catalog.update_playlist(key, name='Работа', track_ids=[two, one])
        self.assertEqual([t['title'] for t in catalog.playlist_tracks(name='работа')['tracks']], ['Second', 'First'])
        catalog.update_playlist(key, remove_track_id=two)
        self.assertEqual(len(catalog.snapshot()['tracks']), 2)
        catalog.delete_song(one)
        self.assertEqual(catalog.playlist_tracks(key)['tracks'], [])
        catalog.delete_playlist(key)
        self.assertEqual(len(catalog.snapshot()['tracks']), 1)

    def test_rejected_update_is_atomic_and_names_are_unique(self):
        key = self.playlist()
        before = catalog.snapshot()
        with self.assertRaises(MusicBrowserError): catalog.update_playlist(key, name='Changed', track_ids=['missing'])
        self.assertEqual(catalog.snapshot(), before)
        with self.assertRaises(MusicBrowserError): catalog.create_playlist('  ДЛЯ  работы ')
        with self.assertRaises(MusicBrowserError): catalog.create_playlist('   ')

    def test_corrupt_catalog_is_never_overwritten(self):
        path = self.root/'player'/'catalog.json'; path.parent.mkdir(parents=True)
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(MusicBrowserError): catalog.save_song(FIRST)
        self.assertEqual(path.read_text(), '{broken')

    def test_parallel_saves_do_not_lose_tracks_or_playlist_members(self):
        key = self.playlist()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: catalog.save_song(f'https://youtu.be/test{i:07d}', f'Song {i}', playlist_id=key), range(16)))
        self.assertEqual(len(results), 16)
        self.assertEqual(len(catalog.snapshot()['tracks']), 16)
        self.assertEqual(len(catalog.playlist_tracks(key)['tracks']), 16)

    def test_bot_playlist_request_only_matches_explicit_commands(self):
        self.assertEqual(mp.parse_playlist_request('включи плейлист Для работы'), 'Для работы')
        self.assertIsNone(mp.parse_playlist_request('расскажи про плейлист'))
        self.assertIsNone(mp.playlist_from_chat('Missing'))
        self.playlist()
        with patch.object(mp, '_chat_api', return_value={'state':'playing'}) as api, patch.object(mp, 'play_playlist') as local:
            self.assertEqual(mp.playlist_from_chat('Для работы')['state'], 'playing')
            api.assert_called_once_with('/api/player/playlists/play', {'name': 'Для работы'})
            local.assert_not_called()


class AutoSaveQueueTests(CatalogFixture):
    def setUp(self):
        super().setUp()
        self.browser = MusicBrowser()
        self.addCleanup(self.browser._worker.shutdown, wait=False)

    def test_search_result_saves_once_and_ignores_ads_and_errors(self):
        b = self.browser
        row = {'url': FIRST, 'media': True, 'ad': True, 'title': 'Advertisement'}
        b._pending_save = (FIRST, 'Search title', 'search')
        b._save_pending(row, 'playing')
        self.assertEqual(catalog.snapshot()['tracks'], [])
        row.update(ad=False, title='SAIKAI - YouTube')
        b._save_pending(row, 'error')
        self.assertEqual(catalog.snapshot()['tracks'], [])
        b._save_pending(row, 'playing')
        b._save_pending(row, 'playing')
        self.assertEqual([t['title'] for t in catalog.snapshot()['tracks']], ['SAIKAI'])

    def test_waits_for_metadata_but_can_use_a_search_title(self):
        b = self.browser
        row = {'url': FIRST, 'media': True, 'ad': False, 'title': 'YouTube'}
        b._pending_save = (FIRST, '', 'link')
        b._save_pending(row, 'playing')
        self.assertEqual(catalog.snapshot()['tracks'], [])
        b._pending_save = (FIRST, 'Found song', 'search')
        b._save_pending(row, 'buffering')
        self.assertEqual(catalog.snapshot()['tracks'][0]['title'], 'Found song')

    def test_playing_an_existing_queue_does_not_resurrect_deleted_links(self):
        data = catalog.save_song(FIRST, 'First')
        catalog.delete_song(data['saved_track_id'])
        self.browser._pending_save = (FIRST, 'First', 'playlist')
        self.browser._save_pending({'url': FIRST, 'media': True, 'ad': False, 'title': 'First'}, 'playing')
        self.assertEqual(catalog.snapshot()['tracks'], [])

    def test_save_failure_does_not_interrupt_playback(self):
        b = self.browser
        b._pending_save = (FIRST, 'Found song', 'search')
        with patch.object(catalog, 'save_song', side_effect=catalog.CatalogError('Disk error')):
            b._save_pending({'url': FIRST, 'media': True, 'ad': False, 'title': 'Song'}, 'playing')
        self.assertEqual(b._save_error, 'Disk error')

    def test_play_snapshot_next_previous_and_boundaries(self):
        key = self.playlist()
        catalog.save_song(FIRST, 'First', playlist_id=key)
        catalog.save_song(SECOND, 'Second', playlist_id=key)
        b = self.browser
        b._page = MagicMock(); b._page.is_closed.return_value = False
        with patch.object(b, '_prepare'), patch.object(b, '_open', return_value={'state':'playing'}) as opened, patch.object(b, '_schedule_playback'), patch.object(b, '_status', return_value={'volume': .5}):
            b._play_playlist(playlist_id=key)
            self.assertEqual(opened.call_args.args[0], FIRST)
            b._control('next')
            self.assertEqual(opened.call_args.args[0], SECOND)
            with self.assertRaises(MusicBrowserError): b._control('next')
            b._control('previous')
            self.assertEqual(opened.call_args.args[0], FIRST)
            with self.assertRaises(MusicBrowserError): b._control('previous')
            b._dispatch('open', {'url': SECOND})
            self.assertIsNone(b._playlist)

    def test_automatic_advance_and_last_track_do_not_need_ui_polling(self):
        b = self.browser
        b._context = MagicMock(); b._page = MagicMock(); b._ended = True; b._monitoring = True
        b._playlist = {'tracks':[{'url':FIRST},{'url':SECOND}], 'index':0, 'complete':False}
        context = b._context
        with patch.object(b, '_status', return_value={'state':'paused'}), patch.object(b, '_playlist_track') as next_track, patch.object(b, '_schedule_playback'):
            b._poll_playback()
            next_track.assert_called_once_with(1)
            b._playlist['index'] = 1
            b._poll_playback()
            context.close.assert_called_once()
            self.assertIsNone(b._playlist)
            self.assertIsNone(b._context)
            self.assertFalse(b._monitoring)

    def test_ad_end_does_not_skip_a_playlist_song(self):
        b = self.browser
        b._context = MagicMock(); b._page = MagicMock(); b._ended = True; b._monitoring = True
        b._playlist = {'tracks': [{'url': FIRST}, {'url': SECOND}], 'index': 0, 'complete': False}
        with patch.object(b, '_status', return_value={'state': 'paused', 'ad': True}), patch.object(b, '_playlist_track') as next_track, patch.object(b, '_schedule_playback'):
            b._poll_playback()
            next_track.assert_not_called()

    def test_single_track_closes_without_ui_and_suspend_cancels_monitor(self):
        b = self.browser
        b._context = MagicMock(); b._page = MagicMock(); b._ended = True; b._monitoring = True
        context = b._context
        with patch.object(b, '_status', return_value={'state': 'paused'}), patch.object(b, '_schedule_playback') as schedule:
            b._poll_playback()
            context.close.assert_called_once()
            schedule.assert_not_called()
        self.assertIsNone(b._context)
        self.assertFalse(b._monitoring)

    def test_finished_file_does_not_close_new_music(self):
        b = self.browser
        b._context = MagicMock(); b._suspended = False
        with patch.object(b, '_status', return_value={'running': True}), patch.object(b, '_close') as close:
            b._dispatch('close_suspended', {})
            close.assert_not_called()
            b._suspended = True
            b._dispatch('close_suspended', {})
            close.assert_called_once()


class CatalogApiTests(CatalogFixture):
    def setUp(self):
        super().setUp()
        async def auth(request: Request):
            if request.headers.get('authorization') != 'Bearer test': raise HTTPException(401)
        self.app = FastAPI(); self.app.include_router(create_router(auth, frozenset()))
        self.client = TestClient(self.app, base_url='http://localhost', client=('127.0.0.1',1234))
        self.addCleanup(self.client.close)
        self.headers = {'authorization':'Bearer test'}

    def test_routes_are_authenticated_local_and_persist(self):
        self.assertEqual(self.client.get('/api/player/saved').status_code, 401)
        with TestClient(self.app, base_url='http://localhost', client=('192.168.1.5',1234)) as remote:
            self.assertEqual(remote.post('/api/player/playlists', json={'name':'Test'}, headers=self.headers).status_code, 403)
        response = self.client.post('/api/player/playlists', json={'name':'Test'}, headers=self.headers)
        key = response.json()['playlist_id']
        response = self.client.post('/api/player/saved', json={'url':FIRST, 'title':'My song','playlist_id':key}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        track = response.json()['tracks'][0]['id']
        self.assertEqual(self.client.get('/api/player/saved', headers=self.headers).json()['playlists'][0]['track_ids'], [track])
        self.assertEqual(self.client.put('/api/player/playlists/'+key, json={'name':'Renamed'}, headers=self.headers).status_code, 200)
        with patch.object(mp, 'play_playlist', return_value={'running':True,'state':'playing'}) as play:
            response = self.client.post('/api/player/playlists/play', json={'playlist_id':key,'track_id':track}, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(play.call_args.kwargs['playlist_id'], key)
        self.assertEqual(self.client.delete('/api/player/saved/'+track, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete('/api/player/playlists/'+key, headers=self.headers).status_code, 200)


if __name__ == '__main__': unittest.main()

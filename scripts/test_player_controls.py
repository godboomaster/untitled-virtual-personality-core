"""Shared controls, browser history, library handoff and stale-tab behavior."""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.features import music_player as mp
from app.features.music_browser import MusicBrowser, MusicBrowserError
from app.features.player_library import LibraryPlayer


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.player = LibraryPlayer()
        self.state = dict(state='playing', title='Uploaded.mp3', volume=.5, position=1, duration=90)

    def test_commands_wait_for_real_audio_ack(self):
        self.player.update('tab', self.state, activate=True)
        result = []
        thread = threading.Thread(target=lambda: result.append(self.player.control('pause')))
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                commands = self.player.update('tab', self.state)['commands']
                if commands: break
                time.sleep(.01)
            self.assertEqual(commands[0]['command'], 'pause')
            self.assertEqual(result, [])
            self.assertEqual(self.player.update('tab', self.state)['commands'], [])
            self.player.update('tab', {**self.state, 'state': 'paused'}, ack=commands[0]['id'])
        finally:
            thread.join(2)
            self.player.deactivate()
        self.assertEqual(result[0]['state'], 'paused')
        self.assertEqual(result[0]['source'], 'library')

    def test_stop_ack_can_release_the_owner_without_timing_out(self):
        self.player.update('tab', self.state, activate=True)
        result = []
        thread = threading.Thread(target=lambda: result.append(self.player.control('stop')))
        thread.start()
        try:
            deadline = time.monotonic() + 2
            commands = []
            while time.monotonic() < deadline:
                commands = self.player.update('tab', self.state)['commands']
                if commands: break
                time.sleep(.01)
            self.assertTrue(commands)
            self.player.update('tab', {**self.state, 'state': 'stopped'}, ack=commands[0]['id'])
        finally:
            thread.join(2)
            self.player.deactivate()
        self.assertFalse(thread.is_alive())
        self.assertFalse(result[0]['running'])
        self.assertEqual(result[0]['state'], 'stopped')

    def test_stale_or_replaced_tab_cannot_reclaim_player(self):
        self.player.update('old', self.state, activate=True)
        self.player.deactivate()
        self.assertFalse(self.player.update('old', self.state)['active'])
        self.player.update('new', self.state, activate=True)
        self.assertFalse(self.player.update('old', self.state)['active'])
        self.player._until = 0
        self.assertIsNone(self.player.status())
        self.assertFalse(self.player.update('new', self.state)['active'])

    def test_shared_control_prefers_library_and_does_not_launch_chrome(self):
        with patch('app.features.player_library.library_player') as library, patch('app.features.music_browser.music_browser') as browser:
            library.status.return_value = {**self.state, 'running': True}
            library.control.return_value = {'state': 'paused'}
            self.assertEqual(mp.control('pause'), {'state': 'paused'})
            library.control.assert_called_once_with('pause', None)
            browser.execute_sync.assert_not_called()

    def test_empty_player_control_does_not_start_browser(self):
        with patch('app.features.player_library.library_player') as library, patch('app.features.music_browser.music_browser') as browser:
            library.status.return_value = None
            browser.execute_sync.return_value = {'running': False, 'state': 'stopped'}
            self.assertFalse(mp.control('resume')['running'])
            browser.execute_sync.assert_called_once_with('status')

    def test_closing_library_also_closes_the_paused_online_browser(self):
        with patch('app.features.player_library.library_player') as library, patch('app.features.music_browser.music_browser') as browser:
            library.status.return_value = {**self.state, 'running': True}
            library.control.return_value = {'running': False, 'state': 'stopped'}
            self.assertFalse(mp.control('stop')['running'])
            library.control.assert_called_once_with('stop', None)
            browser.execute_sync.assert_called_once_with('stop')

    def test_starting_an_uploaded_file_suspends_the_previous_queue(self):
        with patch('app.features.player_library.library_player') as library, patch('app.features.music_browser.music_browser') as browser:
            browser.execute_sync.return_value = {'running': True}
            mp.update_library('tab', self.state, activate=True)
            self.assertEqual(browser.execute_sync.call_args_list[1].args, ('suspend',))
            library.update.assert_called_once_with('tab', self.state, activate=True, ack=None, error='')

    def test_natural_end_releases_owner_and_stale_end_cannot_close_new_file(self):
        self.player.update('old', self.state, activate=True)
        result = self.player.update('old', {**self.state, 'state': 'stopped'})
        self.assertTrue(result['finished'])
        self.assertIsNone(self.player.status())
        self.player.update('new', self.state, activate=True)
        self.assertNotIn('finished', self.player.update('old', {**self.state, 'state': 'stopped'}))
        self.assertTrue(self.player.status()['running'])

    def test_stopped_client_cannot_activate_or_revive_an_expired_lease(self):
        self.assertFalse(self.player.update('old', {**self.state, 'state': 'stopped'}, activate=True)['active'])
        self.assertIsNone(self.player.status())
        self.player.update('old', self.state, activate=True)
        self.player._until = 0
        self.assertNotIn('finished', self.player.update('old', {**self.state, 'state': 'stopped'}, activate=True))

    def test_finished_file_closes_only_its_suspended_browser(self):
        with patch('app.features.player_library.library_player', self.player), patch('app.features.music_browser.music_browser') as browser:
            self.player.update('tab', self.state, activate=True)
            self.assertFalse(mp.update_library('tab', {**self.state, 'state': 'stopped'})['active'])
            browser.execute_sync.assert_called_once_with('close_suspended')
            browser.reset_mock()
            mp.update_library('old', {**self.state, 'state': 'stopped'})
            browser.execute_sync.assert_not_called()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.browser = MusicBrowser()
        self.addCleanup(self.browser._worker.shutdown, wait=False)
        self.browser._page = MagicMock()
        self.browser._page.is_closed.return_value = False

    def test_previous_then_next_preserves_youtube_history(self):
        b = self.browser
        b._history = [('youtube', 'https://www.youtube.com/watch?v=M7lc1UVf-VE'),
                      ('youtube_music', 'https://music.youtube.com/watch?v=0WA2NX4kPSI')]
        b._history_pos = 1
        b._source = 'youtube_music'
        with patch.object(b, '_status', return_value={'volume': .5}), patch.object(b, '_open', return_value={'state': 'playing'}) as opened:
            b._control('previous')
            self.assertEqual(b._history_pos, 0)
            opened.assert_called_with(b._history[0][1], True, .5)
            b._control('next')
            self.assertEqual(b._history_pos, 1)
            opened.assert_called_with(b._history[1][1], True, .5)

    def test_local_queue_bounds_and_switch(self):
        b = self.browser
        b._source = 'local'; b._local_queue = ['/music/one.mp3', '/music/two.mp3']; b._local_index = 0
        with patch.object(b, '_status', return_value={'volume': .5}), patch.object(b, '_local', return_value={'state': 'playing'}) as opened:
            with self.assertRaises(MusicBrowserError): b._control('previous')
            b._control('next')
            opened.assert_called_once_with('/music/two.mp3', True, .5)


if __name__ == '__main__':
    unittest.main()

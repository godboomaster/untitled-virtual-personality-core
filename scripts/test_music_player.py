"""Music command routing, local-first lookup and shared provider settings.

PYTHONPATH=. python -m scripts.test_music_player
No live browser, online searches, or real user files.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.features import music_player as mp, music_browser as mb, file_finder as ff


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(mp, 'data_dir', return_value=self.root)
        p.start(); self.addCleanup(p.stop)

    def test_commands_and_provider_overrides(self):
        cases = {
            'включи Never Gonna Give You Up': ('Never Gonna Give You Up', None),
            'включи песню SAIKAI': ('SAIKAI', None),
            'открой SAIKAI на ютубе': ('SAIKAI', 'youtube'),
            'открой SAIKAI на ютуб музыке': ('SAIKAI', 'youtube_music'),
            'включи SAIKAI на YouTube Music': ('SAIKAI', 'youtube_music'),
            'включи SAIKAI на ютуб мьюзик': ('SAIKAI', 'youtube_music'),
            'play Never Gonna Give You Up on youtube': ('Never Gonna Give You Up', 'youtube'),
            'открой песню Свет': ('Свет', None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                req = mp.parse_request(text)
                self.assertIsNotNone(req)
                self.assertEqual((req.query, req.provider), expected)
        self.assertFalse(mp.parse_request('открой SAIKAI на ютубе').local_first)
        self.assertTrue(mp.parse_request('включи SAIKAI').local_first)

    def test_player_controls_are_exact_and_keep_music_context(self):
        for text, expected in {
            'пауза': ('pause', False), 'пауза в плеере': ('pause', True),
            'поставь музыку на паузу': ('pause', True), 'продолжи': ('resume', False),
            'продолжи музыку': ('resume', True), 'следующий трек': ('next', True),
            'предыдущий': ('previous', False), 'предыдущую песню': ('previous', True),
            'громче': ('volume_up', False), 'сделай музыку тише': ('volume_down', True),
        }.items():
            with self.subTest(text=text): self.assertEqual(mp.parse_control(text), expected)
        for text in ('продолжи писать код', 'следующий шаг задачи', 'тише едешь дальше будешь',
                     'пауза на 10 минут', 'прочитай предыдущий трек', 'сделай экран ярче'):
            self.assertIsNone(mp.parse_control(text), text)

    def test_existing_commands_are_not_stolen(self):
        for text in ('включи звук', 'включи звук на ютубе', 'включи субтитры',
                     'включи режим управления', 'включи камеру', 'включи свет',
                     'включи Google Chrome', 'открой комментарии на ютубе',
                     'открой файл report.mp3', 'поставь таймер на 10 минут',
                     'включи фильм Дюна', 'включи Daft Punk на Spotify',
                     'включи музыку', 'открой ютуб', 'сыграй со мной',
                     'найди SAIKAI на ютубе', 'включи приложение My Player'):
            with self.subTest(text=text):
                self.assertIsNone(mp.parse_request(text, reserved=['Google Chrome', 'приложение My Player']))
        self.assertIsNone(mp.parse_request('включи Custom App', reserved=['Custom App']))

    def test_provider_persists_and_invalid_provider_rejected(self):
        self.assertEqual(mp.settings()['provider'], 'youtube_music')
        mp.set_provider('youtube')
        self.assertEqual(mp.settings()['provider'], 'youtube')
        with self.assertRaises(ValueError): mp.set_provider('spotify')
        self.assertEqual(mp.settings()['provider'], 'youtube')

    def test_local_hit_does_not_start_online_search(self):
        path = self.root/'song.mp3'
        with patch.object(mp, 'find_mp3', return_value=path), patch.object(mb.music_browser, 'execute_sync', return_value={'state':'playing'}) as execute:
            mp.play('song')
            execute.assert_called_once_with('local', path=str(path), headless=True)

    def test_no_hit_uses_selected_or_explicit_provider(self):
        mp.set_provider('youtube')
        with patch.object(mp, 'find_mp3', return_value=None), patch.object(mb.music_browser, 'execute_sync', return_value={}) as execute:
            mp.play('song')
            execute.assert_called_once_with('search', query='song', provider='youtube', headless=True)
            execute.reset_mock()
            mp.play('song', 'youtube_music')
            execute.assert_called_once_with('search', query='song', provider='youtube_music', headless=True)

    def test_disallowed_local_access_skips_lookup(self):
        with patch.object(mp, 'find_mp3') as find, patch.object(mb.music_browser, 'execute_sync', return_value={}):
            mp.play('song', local_first=False)
            find.assert_not_called()

    def test_fast_file_lookup_filters_extension_and_hidden_folders(self):
        music = self.root/'Music'
        music.mkdir()
        (music/'Artist - My Song.mp3').write_bytes(b'ID3test')
        (music/'Artist - Other Song.mp3').write_bytes(b'ID3test')
        (music/'Artist - My Song.txt').write_text('not music')
        (music/'.hidden').mkdir()
        hidden = music/'.hidden'/'Artist - My Song.mp3'
        hidden.write_bytes(b'ID3test')
        with patch.object(Path, 'home', return_value=self.root), patch.object(ff, '_HOME', self.root), \
             patch.object(mp.shutil, 'which', return_value=None):
            self.assertEqual(mp.find_mp3('Artist My Song'), music/'Artist - My Song.mp3')
            self.assertIsNone(mp.find_mp3('Missing Song'))

    def test_index_hit_is_not_enough_without_matching_name_and_extension(self):
        for name in ('Wrong Song.mp3', 'Artist My Song.txt'):
            path = self.root/name; path.write_text('ID3')
            with patch.object(Path, 'home', return_value=self.root), patch.object(ff, '_HOME', self.root), \
                 patch.object(mp.shutil, 'which', return_value='/usr/bin/mdfind'), patch.object(ff, '_read_lines', return_value=[str(path)]):
                self.assertIsNone(mp.find_mp3('Artist My Song'))

    def test_separate_bot_process_forwards_to_matching_api(self):
        (self.root/'link').mkdir(); (self.root/'link'/'core_id').write_text('same-core')
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {'id':'same-core'}
        client.post.return_value.is_success = True
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {'state':'playing','source':'youtube'}
        with patch.object(mp, 'IS_API_PROCESS', False), patch('httpx.Client') as factory, patch.object(mp, 'play') as local:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(mp.play_from_chat('Song')['state'], 'playing')
            local.assert_not_called()
            client.post.assert_called_once()
            self.assertFalse(factory.call_args.kwargs['trust_env'])
            self.assertEqual(client.post.call_args.kwargs['json']['local_first'], True)

    def test_ambiguous_api_reply_never_starts_duplicate_playback(self):
        import httpx
        (self.root/'link').mkdir(); (self.root/'link'/'core_id').write_text('same-core')
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {'id':'same-core'}
        client.post.side_effect = httpx.ReadTimeout('timeout')
        with patch.object(mp, 'IS_API_PROCESS', False), patch('httpx.Client') as factory, patch.object(mp, 'play') as local:
            factory.return_value.__enter__.return_value = client
            with self.assertRaises(mb.MusicBrowserError): mp.play_from_chat('Song')
            local.assert_not_called()

    def test_separate_bot_controls_the_same_api_player(self):
        (self.root/'link').mkdir(); (self.root/'link'/'core_id').write_text('same-core')
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {'id': 'same-core'}
        client.post.return_value.is_success = True
        client.post.return_value.json.return_value = {'running': True, 'state': 'paused'}
        with patch.object(mp, 'IS_API_PROCESS', False), patch('httpx.Client') as factory, patch.object(mp, 'control') as local:
            factory.return_value.__enter__.return_value = client
            self.assertEqual(mp.control_from_chat('pause')['state'], 'paused')
            self.assertEqual(client.post.call_args.args[0], '/api/player/browser/control')
            self.assertEqual(client.post.call_args.kwargs['json'], {'command': 'pause'})
            local.assert_not_called()

    def test_different_core_is_not_controlled(self):
        (self.root/'link').mkdir(); (self.root/'link'/'core_id').write_text('this-core')
        client = MagicMock()
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {'id':'other-core'}
        with patch.object(mp, 'IS_API_PROCESS', False), patch('httpx.Client') as factory, patch.object(mp, 'play', return_value={'state':'playing'}) as local:
            factory.return_value.__enter__.return_value = client
            mp.play_from_chat('Song')
            client.post.assert_not_called()
            local.assert_called_once_with('Song', None, local_first=True)


class BotMusicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.bot_instance import BotInstance
        cls.BotInstance = BotInstance

    def bot(self):
        b = self.BotInstance.__new__(self.BotInstance)
        b.features = {}; b.owner = 'owner'; b.web_single_user = False
        b.computer_control = SimpleNamespace(apps={'Custom App':'app'}, tasks={}, sites={})
        b.scenario_manager = None
        b._cc_command_text = lambda text: text
        return b

    def test_owner_request_uses_shared_player_without_control_mode(self):
        b = self.bot()
        with patch.object(mp, 'play_from_chat', return_value={'state':'playing','source':'youtube','title':'Song'}) as play:
            self.assertIn('Song', b._music_player_turn('включи Song', 'owner', 'owner', 'ru'))
            play.assert_called_once_with('Song', None, local_first=True)

    def test_controls_only_capture_an_active_player(self):
        b = self.bot()
        with patch.object(mp, 'control_from_chat', return_value={'running': True, 'state': 'paused'}) as control:
            self.assertIn('паузу', b._music_player_turn('пауза', 'owner', 'owner'))
            control.assert_called_once_with('pause')
        with patch.object(mp, 'control_from_chat', return_value={'running': False}):
            self.assertIsNone(b._music_player_turn('продолжи', 'owner', 'owner'))
            self.assertIn('пуст', b._music_player_turn('продолжи музыку', 'owner', 'owner'))

    def test_task_dialog_keeps_bare_pause_and_continue(self):
        b = self.bot()
        b.task_agent = SimpleNamespace(active=lambda chat: True)
        with patch.object(mp, 'control_from_chat', return_value={'running': True, 'state': 'paused'}) as control:
            for text in ('пауза', 'продолжи', 'громче'):
                self.assertIsNone(b._music_player_turn(text, 'owner', 'owner'))
            control.assert_not_called()
            self.assertIn('паузу', b._music_player_turn('пауза в плеере', 'owner', 'owner'))
            control.assert_called_once_with('pause')

    def test_scenario_recording_keeps_even_explicit_music_commands(self):
        b = self.bot()
        b.scenario_manager = SimpleNamespace(recording=lambda chat: True)
        with patch.object(mp, 'control_from_chat') as control:
            self.assertIsNone(b._music_player_turn('пауза в плеере', 'owner', 'owner'))
            control.assert_not_called()

    def test_real_command_normalizer_handles_persona_name_and_politeness(self):
        b = self.bot()
        b.persona_name = 'Коннор'
        b._cc_command_text = self.BotInstance._cc_command_text.__get__(b)
        with patch.object(mp, 'play_from_chat', return_value={'state':'playing','source':'youtube','title':'SAIKAI'}) as play:
            response=b._music_player_turn('Коннор, можешь включить песню SAIKAI на ютубе, пожалуйста?', 'owner', 'owner', 'ru')
            self.assertIn('SAIKAI',response)
            play.assert_called_once_with('SAIKAI','youtube',local_first=True)

    def test_explicit_open_on_site_preserves_online_intent(self):
        with patch.object(mp, 'play_from_chat', return_value={'state':'playing','source':'youtube','title':'Song'}) as play:
            self.bot()._music_player_turn('открой Song на ютубе', 'owner', 'owner')
            play.assert_called_once_with('Song','youtube',local_first=False)

    def test_authorization_and_other_commands(self):
        from app import bot_instance as bi
        b = self.bot()
        with patch.object(mp, 'play_from_chat') as play:
            self.assertIsNone(b._music_player_turn('включи Song', 'other', 'other'))
            self.assertIsNone(b._music_player_turn('включи Song', 'owner', '-123'))
            self.assertIsNone(b._music_player_turn('включи звук', 'owner', 'owner'))
            self.assertIsNone(b._music_player_turn('включи Custom App', 'owner', 'owner'))
            bi._SKIN_TURN.on = True
            try: self.assertIsNone(b._music_player_turn('включи Song', 'owner', 'owner'))
            finally: bi._SKIN_TURN.on = False
            b.scenario_manager = SimpleNamespace(recording=lambda chat: True, active=lambda chat: False)
            self.assertIsNone(b._music_player_turn('включи Song', 'owner', 'owner'))
            play.assert_not_called()


if __name__ == '__main__':
    unittest.main()

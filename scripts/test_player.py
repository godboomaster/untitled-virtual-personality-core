"""Player path import: actual HTTP route, validation, auth and local access.

Run: PYTHONPATH=. python -m scripts.test_player
"""

import tempfile
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import player_api
from app.api.security import BodySizeLimit
from app.features.music_browser import MusicBrowser, MusicBrowserError, configure_headless_page, youtube_url


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.audio = self.root / "песня.wav"
        with wave.open(str(self.audio), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\0\0" * 800)

        async def auth(request: Request):
            if request.headers.get("authorization") != "Bearer test":
                raise HTTPException(401, "auth required")

        self.app = FastAPI()
        self.app.include_router(player_api.create_router(auth, frozenset()))
        self.app.add_middleware(BodySizeLimit, rules=[
            ("POST", r"/api/player/import-path", player_api.MAX_BODY_BYTES),
            ("POST", r"/api/player/browser/(play|control)", player_api.MAX_BODY_BYTES),
            ("POST", r"/api/player/play", player_api.MAX_BODY_BYTES),
            ("POST", r"/api/player/library", player_api.MAX_BODY_BYTES),
            ("PUT", r"/api/player/settings", player_api.MAX_BODY_BYTES),
        ])
        self.client = TestClient(self.app, base_url="http://localhost", client=("127.0.0.1", 12345))
        self.addCleanup(self.client.close)

    def post(self, path, **kwargs):
        return self.client.post("/api/player/import-path", json={"path": str(path)},
                                headers={"authorization": "Bearer test", **kwargs})

    def test_audio_bytes_and_type(self):
        response = self.post(self.audio)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.audio.read_bytes())
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_quoted_path(self):
        self.assertEqual(self.post(f'"{self.audio}"').status_code, 200)

    def test_auth_required(self):
        response = self.client.post("/api/player/import-path", json={"path": str(self.audio)})
        self.assertEqual(response.status_code, 401)

    def test_remote_cannot_read_local_audio(self):
        with TestClient(self.app, base_url="http://localhost", client=("192.168.1.7", 12345)) as remote:
            response = remote.post("/api/player/import-path", json={"path": str(self.audio)},
                                   headers={"authorization": "Bearer test"})
            self.assertEqual(response.status_code, 403)

    def test_foreign_origin_and_proxy_rejected(self):
        self.assertEqual(self.post(self.audio, origin="https://example.com").status_code, 403)
        self.assertEqual(self.post(self.audio, **{"x-forwarded-for": "1.2.3.4"}).status_code, 403)

    def test_relative_missing_url_directory(self):
        for path in ("song.wav", self.root / "missing.wav", "https://example.com/a.mp3", self.root):
            with self.subTest(path=path):
                self.assertIn(self.post(path).status_code, (400, 415))

    def test_non_audio_and_disguised_text(self):
        for name in ("secret.txt", "secret.mp3", "secret.wav"):
            path = self.root / name
            path.write_text("not an audio file")
            self.assertEqual(self.post(path).status_code, 415)

    def test_symlink_to_non_audio(self):
        secret = self.root / "secret.txt"
        secret.write_text("not audio")
        alias = self.root / "alias.wav"
        alias.symlink_to(secret)
        self.assertEqual(self.post(alias).status_code, 415)

    def test_empty_and_large_files(self):
        path = self.root / "empty.wav"
        path.touch()
        self.assertEqual(self.post(path).status_code, 400)
        with path.open("wb") as out:
            out.truncate(player_api.MAX_AUDIO_BYTES + 1)
        self.assertEqual(self.post(path).status_code, 413)

    def test_body_limit_and_invalid_input(self):
        self.assertEqual(self.post("x" * (player_api.MAX_BODY_BYTES + 1)).status_code, 413)
        self.assertEqual(self.post("").status_code, 422)
        self.assertEqual(self.post("\0bad").status_code, 400)

    def test_browser_routes_require_local_auth(self):
        for path, body in (("/browser/play", {"url": "https://youtu.be/M7lc1UVf-VE"}),
                           ("/browser/control", {"command": "stop"})):
            endpoint = "/api/player" + path
            self.assertEqual(self.client.post(endpoint, json=body).status_code, 401)
            with TestClient(self.app, base_url="http://localhost", client=("192.168.1.7", 12345)) as remote:
                self.assertEqual(remote.post(endpoint, json=body, headers={"authorization": "Bearer test"}).status_code, 403)
        self.assertEqual(self.client.get("/api/player/browser").status_code, 401)

    def test_browser_commands_validate_numbers(self):
        headers = {"authorization": "Bearer test"}
        for body in ({"command": "volume"}, {"command": "seek"}, {"command": "volume", "value": 50},
                     {"command": "seek", "value": -1}, {"command": "execute"}):
            self.assertEqual(self.client.post("/api/player/browser/control", json=body, headers=headers).status_code, 422)
        self.assertEqual(self.client.post("/api/player/browser/play", json={"url": "https://youtu.be/M7lc1UVf-VE", "volume": 2}, headers=headers).status_code, 422)

    def test_shared_commands_dispatch_to_same_player(self):
        headers = {"authorization": "Bearer test"}
        with patch('app.features.music_player.control', return_value={'running': True, 'state': 'playing'}) as control:
            for command in ('pause', 'resume', 'next', 'previous', 'volume_up', 'volume_down'):
                response = self.client.post('/api/player/browser/control', json={'command': command}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                control.assert_called_with(command=command, value=None)

    def test_library_control_link_is_local_authenticated_and_validated(self):
        body = {'client': 'tab-123', 'activate': True, 'status': {'state': 'playing',
                'title': 'test.mp3', 'volume': .5, 'position': 0, 'duration': 20}}
        headers = {"authorization": "Bearer test"}
        self.assertEqual(self.client.post('/api/player/library', json=body).status_code, 401)
        with TestClient(self.app, base_url='http://localhost', client=('192.168.1.7', 12345)) as remote:
            self.assertEqual(remote.post('/api/player/library', json=body, headers=headers).status_code, 403)
        with patch('app.features.music_player.update_library', return_value={'active': True, 'commands': []}) as update:
            self.assertEqual(self.client.post('/api/player/library', json=body, headers=headers).status_code, 200)
            self.assertEqual(update.call_args.kwargs['status']['title'], 'test.mp3')
            body['activate'] = False
            body['status']['state'] = 'stopped'
            self.assertEqual(self.client.post('/api/player/library', json=body, headers=headers).status_code, 200)
            self.assertEqual(update.call_args.kwargs['status']['state'], 'stopped')
        body['status']['volume'] = 5
        self.assertEqual(self.client.post('/api/player/library', json=body, headers=headers).status_code, 422)

    def test_browser_requests_dispatch_and_report_errors(self):
        headers = {"authorization": "Bearer test"}
        with patch.object(player_api.music_browser, "execute", new=AsyncMock(return_value={"running": True})) as call:
            response = self.client.post("/api/player/browser/play", json={"url": "https://youtu.be/M7lc1UVf-VE"}, headers=headers)
            self.assertEqual(response.status_code, 200)
            call.assert_awaited_once_with("open", url="https://youtu.be/M7lc1UVf-VE", headless=True, volume=.5)
        with patch("app.features.music_player.player_status", side_effect=MusicBrowserError("playback failed")):
            response = self.client.get("/api/player/browser", headers=headers)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "playback failed")

    def test_supported_youtube_links(self):
        self.assertEqual(youtube_url("https://youtu.be/M7lc1UVf-VE?t=30&redirect=bad"), "https://www.youtube.com/watch?v=M7lc1UVf-VE&t=30")
        self.assertEqual(youtube_url("https://music.youtube.com/watch?v=M7lc1UVf-VE"), "https://music.youtube.com/watch?v=M7lc1UVf-VE")
        self.assertEqual(youtube_url("youtube.com/shorts/M7lc1UVf-VE"), "https://www.youtube.com/watch?v=M7lc1UVf-VE")
        self.assertEqual(youtube_url("https://www.youtube.com/playlist?list=PL123456789"), "https://www.youtube.com/playlist?list=PL123456789")

    def test_arbitrary_targets_rejected(self):
        for url in ("file:///etc/passwd", "https://localhost", "https://youtube.com.evil.com/watch?v=M7lc1UVf-VE",
                    "https://user:pass@youtube.com/watch?v=M7lc1UVf-VE", "http://youtube.com/watch?v=M7lc1UVf-VE",
                    "https://www.youtube.com:9222/watch?v=M7lc1UVf-VE", "https://youtube.com/redirect?q=https://localhost",
                    "https://youtube.com/watch?v=../foo", "https://youtube.com/watch?v=M7lc1UVf-VE\n&list=bad"):
            with self.subTest(url=url), self.assertRaises(MusicBrowserError):
                youtube_url(url)

    def test_status_and_stop_do_not_launch_a_browser(self):
        async def scenario():
            browser = MusicBrowser()
            try:
                with patch.object(browser, "_launch") as launch:
                    self.assertFalse((await browser.execute("status"))["running"])
                    self.assertFalse((await browser.execute("stop"))["running"])
                    launch.assert_not_called()
            finally:
                await browser.shutdown()
        asyncio.run(scenario())

    def test_browser_launch_is_audible_and_cleanup_is_owned(self):
        browser = MusicBrowser()
        playwright = MagicMock()
        context = playwright.chromium.launch_persistent_context.return_value
        context.pages = [MagicMock()]
        with patch("app.features.music_browser.data_dir", return_value=self.root), \
             patch("playwright.sync_api.sync_playwright") as start:
            start.return_value.start.return_value = playwright
            browser._launch(True)
            kwargs = playwright.chromium.launch_persistent_context.call_args.kwargs
            self.assertTrue(kwargs["headless"])
            self.assertEqual(kwargs["ignore_default_args"], ["--mute-audio", "--enable-automation"])
            self.assertIn("--disable-blink-features=AutomationControlled", kwargs["args"])
            self.assertIn(str(self.root), kwargs["user_data_dir"])
            browser._close()
            context.close.assert_called_once()
            playwright.stop.assert_called_once()
        asyncio.run(browser.shutdown())

    def test_music_compatibility_preserves_real_chrome_version(self):
        page = MagicMock()
        metadata = {"platform": "macOS", "platformVersion": "26.5.1", "architecture": "arm",
                    "bitness": "64", "model": "", "mobile": False, "wow64": False,
                    "brands": [{"brand": "Google Chrome", "version": "157"}],
                    "fullVersionList": [{"brand": "Google Chrome", "version": "157.0.8103.78"}]}
        page.evaluate.return_value = {
            "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) HeadlessChrome/157.0.8103.78 Safari/537.36",
            "platform": "MacIntel", "metadata": metadata,
        }
        session = configure_headless_page(page)
        self.assertIs(session, page.context.new_cdp_session.return_value)
        session.send.assert_called_once_with("Emulation.setUserAgentOverride", {
            "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/157.0.8103.78 Safari/537.36",
            "platform": "MacIntel", "userAgentMetadata": metadata,
        })
        page.context.new_page.assert_not_called()

    def test_missing_chrome_falls_back_to_full_chromium(self):
        from playwright.sync_api import Error
        browser = MusicBrowser()
        playwright = MagicMock()
        context = MagicMock()
        context.pages = [MagicMock()]
        launch = playwright.chromium.launch_persistent_context
        launch.side_effect = [Error("Executable doesn't exist"), context]
        try:
            with patch("app.features.music_browser.data_dir", return_value=self.root), \
                 patch("playwright.sync_api.sync_playwright") as start:
                start.return_value.start.return_value = playwright
                browser._launch(True)
                self.assertEqual([c.kwargs["channel"] for c in launch.call_args_list], ["chrome", "chromium"])
                self.assertEqual(launch.call_args.kwargs["ignore_default_args"], ["--mute-audio", "--enable-automation"])
        finally:
            asyncio.run(browser.shutdown())

    def test_client_hints_on_blank_page_read_secure_native_probe(self):
        page = MagicMock()
        page.evaluate.return_value = {"ua": "HeadlessChrome/157.0.0.0", "platform": "MacIntel", "metadata": None}
        probe = page.context.new_page.return_value
        metadata = {"platform": "macOS", "architecture": "arm", "platformVersion": "26.5.1", "model": "", "mobile": False}
        probe.evaluate.return_value = {"ua": "HeadlessChrome/157.0.0.0", "platform": "MacIntel", "metadata": metadata}
        session = configure_headless_page(page)
        probe.goto.assert_called_once_with("chrome://version", wait_until="domcontentloaded", timeout=5000)
        probe.close.assert_called_once()
        self.assertEqual(session.send.call_args.args[1]["userAgentMetadata"], metadata)
        page.goto.assert_not_called()

    def test_player_provider_settings_and_song_api(self):
        from app.features import music_player
        headers = {"authorization": "Bearer test"}
        with patch.object(music_player, "data_dir", return_value=self.root):
            self.assertEqual(self.client.put("/api/player/settings", json={"provider":"youtube"}, headers=headers).json(), {"provider":"youtube"})
            self.assertEqual(self.client.get("/api/player/settings", headers=headers).json(), {"provider":"youtube"})
            self.assertEqual(self.client.put("/api/player/settings", json={"provider":"spotify"}, headers=headers).status_code, 422)
        with patch.object(music_player, "play", return_value={"running": True, "source": "local"}) as play:
            result = self.client.post("/api/player/play", json={"query":"Song", "provider":"youtube_music"}, headers=headers)
            self.assertEqual(result.status_code, 200)
            play.assert_called_once_with(query="Song", provider="youtube_music", headless=True, volume=None, local_first=True)
        with TestClient(self.app, base_url="http://localhost", client=("192.168.1.7",12345)) as remote, patch.object(music_player, "play") as play:
            self.assertEqual(remote.post("/api/player/play", json={"query":"Song"}, headers=headers).status_code, 403)
            self.assertEqual(remote.put("/api/player/settings", json={"provider":"youtube"}, headers=headers).status_code, 403)
            play.assert_not_called()

    def test_missing_hints_never_creates_inconsistent_browser_identity(self):
        page = MagicMock()
        page.evaluate.return_value = {"ua": "HeadlessChrome/157.0.0.0", "platform": "MacIntel", "metadata": None}
        page.context.new_page.return_value.evaluate.return_value = page.evaluate.return_value
        with self.assertRaises(MusicBrowserError):
            configure_headless_page(page)
        page.context.new_page.return_value.close.assert_called_once()
        page.context.new_cdp_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()

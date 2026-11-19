"""Dedicated, audible Chrome player, separate from the agent's browser pools.

All synchronous Playwright objects live on one worker thread. The user starts
playback explicitly; status polling never launches a browser. Closing the player
or shutting down the API closes only this context and its dedicated profile.
"""

import asyncio
import html
import json
import logging
import re
import stat
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

from app.core.paths import data_dir
from app.core.atomic_io import atomic_write_text

MEDIA_BROWSER_ARGS = ("--autoplay-policy=no-user-gesture-required",
                      "--disable-blink-features=AutomationControlled")
MEDIA_IGNORED_DEFAULT_ARGS = ("--mute-audio", "--enable-automation")
logger = logging.getLogger(__name__)
# Stop service autoplay for every track. Only our explicit playlist timer may
# advance automatically; the capture listener also blocks delayed play attempts.
QUEUE_INIT_JS = """window.__vpcPlaybackEnded=false;
window.addEventListener('ended', event => {
  if(event.target instanceof HTMLMediaElement
     && !document.querySelector('.ad-showing')) {
    event.stopImmediatePropagation();
    event.target.pause();
    window.__vpcPlaybackEnded=true;
  }
},true);"""
QUEUE_INIT_JS += """window.addEventListener('play', event => {
  if(window.__vpcPlaybackEnded
     && event.target instanceof HTMLMediaElement) {
    event.target.pause();event.stopImmediatePropagation();
  }
},true);"""


class MusicBrowserError(RuntimeError):
    pass


def youtube_url(raw: str) -> str:
    value = raw.strip()
    if any(c in value for c in ("\r", "\n", "\0")):
        raise MusicBrowserError("Ссылка содержит недопустимые символы")
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or parsed.hostname not in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}):
            raise ValueError()
        query = parse_qs(parsed.query)
        host = "music.youtube.com" if parsed.hostname == "music.youtube.com" else "www.youtube.com"
        video = (query.get("v") or [""])[0]
        playlist = (query.get("list") or [""])[0]
        if parsed.hostname == "youtu.be":
            video = parsed.path.strip("/")
        elif parsed.path.startswith(("/shorts/", "/embed/")):
            video = parsed.path.split("/")[2]
        elif parsed.path not in ("/watch", "/playlist"):
            raise ValueError()
        if video and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            raise ValueError()
        if playlist and not re.fullmatch(r"[A-Za-z0-9_-]{6,128}", playlist):
            raise ValueError()
        if not video and not playlist:
            raise ValueError()
        args = {"v": video} if video else {}
        if playlist:
            args["list"] = playlist
        # Retain an explicit start offset, never arbitrary redirect parameters.
        offset = (query.get("t") or [""])[0]
        if re.fullmatch(r"[0-9hms]{1,20}", offset):
            args["t"] = offset
        return f"https://{host}/{'watch' if video else 'playlist'}?{urlencode(args)}"
    except (ValueError, IndexError):
        raise MusicBrowserError("Укажите ссылку на видео или плейлист YouTube / YouTube Music") from None


STATE_JS = """() => {
  const media = [...document.querySelectorAll('video,audio')];
  const v = media.find(v=>!v.paused) ?? media[0];
  const error = document.querySelector('.ytp-error-content-wrap,yt-playability-error-supported-renderers,#error-screen');
  const visibleError = error && error.getBoundingClientRect().height > 0 ? error.innerText : '';
  const body = !v ? (document.body?.innerText || '').slice(0,1800) : '';
  const unsupported = /not supported|isn't supported|не поддерживается/i.test(body) ? body.slice(0,600) : '';
  return {title:document.title, url:location.href, media:!!v,
    paused:v?.paused ?? true, ended:v?.ended ?? false, ready:v?.readyState ?? 0,
    position:v?.currentTime ?? 0, duration:Number.isFinite(v?.duration) ? v.duration : 0,
    volume:v?.volume ?? 0, muted:v?.muted ?? false,
    error:v?.error?.message || visibleError || unsupported || '', ad:!!document.querySelector('.ad-showing')};
}"""


def configure_headless_page(page):
    """Keep Chrome's real identity and Client Hints when changing its name.

    Overriding only userAgent disables navigator.userAgentData and Sec-CH-UA
    headers in Chromium. YouTube's player and its media requests then see
    inconsistent browser capabilities.
    """
    identity_js = """async () => ({
        ua: navigator.userAgent, platform: navigator.platform,
        metadata: navigator.userAgentData ? await navigator.userAgentData.getHighEntropyValues([
            'architecture','bitness','fullVersionList','model','platformVersion','wow64'
        ]) : null
    })"""
    identity = page.evaluate(identity_js)
    if not isinstance(identity, dict):
        return None
    ua = identity.get("ua", "")
    if "HeadlessChrome/" in ua:
        if not identity.get("metadata"):
            # about:blank has an opaque, insecure origin: UA Client Hints are
            # unavailable there. Read them on Chrome's own secure page without
            # contacting a website or navigating the user's media tab.
            probe = page.context.new_page()
            try:
                probe.goto("chrome://version", wait_until="domcontentloaded", timeout=5000)
                identity = probe.evaluate(identity_js)
            finally:
                probe.close()
            ua = identity["ua"]
        if not identity.get("metadata"):
            raise MusicBrowserError("Chrome не предоставил сведения о браузере для запуска плеера")
        options = {"userAgent": ua.replace("HeadlessChrome/", "Chrome/"),
                   "platform": identity["platform"]}
        metadata = identity.get("metadata")
        if metadata:
            metadata = dict(metadata)
            for key in ("brands", "fullVersionList"):
                if key in metadata:
                    metadata[key] = [{**brand, "brand": brand["brand"].replace("HeadlessChrome", "Chrome")}
                                     for brand in metadata[key]]
            options["userAgentMetadata"] = metadata
        cdp = page.context.new_cdp_session(page)
        cdp.send("Emulation.setUserAgentOverride", options)
        return cdp
    return None


class MusicBrowser:
    def __init__(self):
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-browser")
        self._playwright = None
        self._context = None
        self._page = None
        self._cdp = None
        self._headless = True
        self._volume = .5
        self._url = ""
        self._source = "youtube"
        self._history = []
        self._history_pos = -1
        self._replaying = False
        self._local_queue = []
        self._local_index = -1
        self._playlist = None
        self._queue_timer = None
        self._monitoring = False
        self._suspended = False
        self._pending_save = None
        self._save_error = ''
        self._ended = False

    def execute_sync(self, action, **kwargs):
        """Chat runs on a regular worker; share the same browser/thread as HTTP."""
        try:
            return self._worker.submit(self._dispatch, action, kwargs).result()
        except MusicBrowserError:
            raise
        except Exception as e:
            raise MusicBrowserError("Не удалось управлять музыкальным браузером") from e

    async def execute(self, action, **kwargs):
        try:
            return await asyncio.wrap_future(self._worker.submit(self._dispatch, action, kwargs))
        except MusicBrowserError:
            raise
        except Exception as e:
            # Playwright may include launch flags and filesystem paths in errors.
            raise MusicBrowserError("Не удалось управлять музыкальным браузером. Проверьте Chrome и откройте видимое окно для входа.") from e

    def _dispatch(self, action, args):
        if action == 'open':
            youtube_url(args['url'])  # Reject bad links before replacing an active queue.
        if action == 'suspend':
            self._clear_playlist()
            self._suspended = True
            return self._control('pause') if self._context else self._status()
        if action == 'close_suspended':
            if self._suspended:
                self._close()
            return self._status()
        if action in ("open", "search", "local", "stop"):
            from app.features.player_library import library_player
            library_player.deactivate()
            self._clear_playlist()
            self._pending_save = None
            self._save_error = ''
        if action == "open":
            return self._open(**args, save_origin='link')
        if action == 'playlist':
            return self._play_playlist(**args)
        if action == "search":
            return self._search(**args)
        if action == "local":
            return self._local(**args)
        if action == "control":
            return self._control(**args)
        if action == "status":
            return self._status()
        if action == "stop":
            self._close()
            return self._status()
        raise MusicBrowserError("Неизвестное действие плеера")

    def _launch(self, headless):
        from playwright.sync_api import Error, sync_playwright

        profile = (data_dir() / "player" / "chrome-profile").resolve()
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._playwright = sync_playwright().start()
        options = dict(user_data_dir=str(profile), headless=headless,
            # Test-automation identity makes YouTube reject stream continuation
            # (ump.spsrejectfailure) after the initial buffered segment. This
            # dedicated media browser needs the normal Chrome media environment.
            ignore_default_args=list(MEDIA_IGNORED_DEFAULT_ARGS),
            args=list(MEDIA_BROWSER_ARGS),
            viewport={"width": 1280, "height": 720}, accept_downloads=False)
        try:
            try:
                self._context = self._playwright.chromium.launch_persistent_context(channel="chrome", **options)
            except Error as e:
                if "executable" not in str(e).lower():
                    raise
                # Use the full bundled Chromium/new headless, not headless-shell:
                # the media environment and native Client Hints probe need it.
                self._context = self._playwright.chromium.launch_persistent_context(channel="chromium", **options)
            self._headless = headless
            self._context.set_default_timeout(5000)
            self._context.add_init_script(QUEUE_INIT_JS)
            self._context.add_init_script(f"window.__vpcMusicVolume={self._volume}; for(const name of ['play','volumechange']) document.addEventListener(name, e => {{ if(e.target instanceof HTMLMediaElement && e.target.volume!==window.__vpcMusicVolume) e.target.volume=window.__vpcMusicVolume; }}, true)")
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._configure_page()
        except MusicBrowserError:
            self._close()
            raise
        except Exception:
            self._close()
            raise MusicBrowserError("Не удалось запустить Chrome. Установите Google Chrome или выполните python -m playwright install chromium.") from None

    def _prepare(self, headless, volume=None):
        if volume is not None:
            self._volume = max(0., min(1., float(volume)))
        if self._context and self._headless != headless:
            self._close()
        if not self._context:
            self._launch(headless)
        if not self._page or self._page.is_closed():
            self._page = self._context.new_page()
            self._configure_page()

    def _open(self, url, headless=True, volume=None, *, save_title='', save_origin=None):
        url = youtube_url(url)
        self._prepare(headless, volume)
        self._url = url
        self._save_error = ''
        self._pending_save = (url, save_title, save_origin) if save_origin else None
        self._source = "youtube_music" if urlsplit(url).hostname == "music.youtube.com" else "youtube"
        self._suspended = False
        from playwright.sync_api import TimeoutError
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=25000)
            self._page.wait_for_selector("video,audio", state="attached", timeout=10000)
        except TimeoutError:
            # Consent/login/network pages remain available in the visible mode.
            self._monitoring = True
            self._schedule_playback()
            return self._status()
        self._evaluate_ready("""async volume => {
            window.__vpcPlaybackEnded=false;
            const autoplay=document.querySelector('.ytp-autonav-toggle-button');
            if(autoplay?.getAttribute('aria-checked')==='true')autoplay.click();
            window.__vpcMusicVolume=volume;
            const v=document.querySelector('video,audio');
            if(v){v.volume=volume;v.muted=false;try{await Promise.race([v.play(),new Promise(r=>setTimeout(r,5000))]);}catch{}}
        }""", self._volume)
        self._monitoring = True
        self._schedule_playback()
        return self._status()

    def _search(self, query, provider, headless=True, volume=None):
        if provider not in ("youtube", "youtube_music"):
            raise MusicBrowserError("Неизвестный музыкальный сервис")
        self._prepare(headless, volume)
        self._source = provider
        self._url = ""
        if provider == "youtube_music":
            url = "https://music.youtube.com/search?" + urlencode({"q": query})
            selector = 'ytmusic-card-shelf-renderer a[href*="watch?"],ytmusic-responsive-list-item-renderer a[href*="watch?"],ytmusic-two-row-item-renderer a[href*="watch?"]'
        else:
            url = "https://www.youtube.com/results?" + urlencode({"search_query": query})
            selector = 'a#video-title[href*="watch?"],a#video-title-link[href*="watch?"],a.ytLockupMetadataViewModelTitle[href*="watch?"]'
        self._page.goto(url, wait_until="domcontentloaded", timeout=25000)
        scope = self._page.locator("ytmusic-search-page") if provider == "youtube_music" else self._page
        if provider == "youtube_music" and not scope.count():
            scope = self._page
        from playwright.sync_api import TimeoutError
        try:
            scope.locator(selector).first.wait_for(state="attached", timeout=12000)
        except TimeoutError:
            raise MusicBrowserError("Поиск не нашёл видео или трек. Проверьте запрос; если нужен вход, снимите «Без окна браузера» и повторите поиск.") from None
        results = scope.locator(selector).evaluate_all("""nodes => nodes.map(a=>({
            url:a.href,title:a.getAttribute('title')||a.textContent.trim()
        })).filter(x=>x.title).slice(0,20)""")
        # The service ranks its own results; retain that order and reject every
        # unrelated/unsafe link before navigating. No LLM or external search API.
        for result in results:
            try:
                target = youtube_url(result["url"])
            except MusicBrowserError:
                continue
            video = parse_qs(urlsplit(target).query).get("v")
            if not video:
                continue
            host = "music.youtube.com" if provider == "youtube_music" else "www.youtube.com"
            target = f"https://{host}/watch?" + urlencode({"v": video[0]})
            return self._open(target, headless, self._volume, save_title=result['title'], save_origin='search')
        raise MusicBrowserError("В результатах нет доступного видео или трека")

    def _local(self, path, headless=True, volume=None):
        from app.features.file_finder import path_allowed
        try:
            file = Path(path).expanduser().resolve(strict=True)
            if file.suffix.lower() != ".mp3" or not path_allowed(str(file)) or not stat.S_ISREG(file.stat().st_mode):
                raise MusicBrowserError("Недопустимый MP3-файл")
            with file.open("rb") as source:
                header = source.read(3)
            if not (header.startswith(b"ID3") or len(header) >= 2 and header[0] == 255 and header[1] & 224 == 224):
                raise MusicBrowserError("Файл не содержит MP3-аудио")
        except (OSError, ValueError):
            raise MusicBrowserError("Не удалось открыть MP3-файл") from None
        self._prepare(headless, volume)
        self._local_queue = sorted((p.resolve() for p in file.parent.iterdir()
            if p.suffix.lower() == '.mp3' and p.is_file() and path_allowed(str(p))), key=lambda p: p.name.casefold())[:1000]
        if file not in self._local_queue:
            self._local_queue.append(file)
        self._local_index = self._local_queue.index(file)
        player = (data_dir() / "player" / "local-player.html").resolve()
        atomic_write_text(player, '<!doctype html><meta charset="utf-8"><title>' + html.escape(file.name)
            + '</title><audio controls></audio><script>document.querySelector("audio").src='
            + json.dumps(file.as_uri()) + ';</script>')
        self._url = file.as_uri()
        self._source = "local"
        self._suspended = False
        self._page.goto(player.as_uri(), wait_until="domcontentloaded", timeout=15000)
        self._page.evaluate("""async volume => {window.__vpcMusicVolume=volume;
            const v=document.querySelector('audio');v.volume=volume;
            await Promise.race([v.play(),new Promise(r=>setTimeout(r,5000))]);}""", self._volume)
        self._monitoring = True
        self._schedule_playback()
        return self._status()

    def _configure_page(self):
        if self._headless:
            self._cdp = configure_headless_page(self._page)

    def _evaluate_ready(self, expression, arg=None):
        """Read/setup operations may race a service redirect after DOMContentLoaded.
        Do not use for next/previous clicks, which must never be retried twice.
        """
        from playwright.sync_api import Error
        for attempt in range(3):
            try:
                return self._page.evaluate(expression, arg)
            except Error as e:
                if attempt == 2 or not any(s in str(e).lower() for s in ('execution context was destroyed', 'cannot find context')):
                    raise
                self._page.wait_for_load_state('domcontentloaded', timeout=10000)

    def _play_playlist(self, playlist_id=None, name=None, track_id=None, headless=True, volume=None):
        from app.features.music_catalog import playlist_tracks
        from app.features.player_library import library_player
        playlist = playlist_tracks(playlist_id, name)
        if not playlist['tracks']:
            raise MusicBrowserError('Плейлист пуст. Добавьте песни.')
        index = 0 if track_id is None else next((i for i, t in enumerate(playlist['tracks']) if t['id'] == track_id), -1)
        if index < 0:
            raise MusicBrowserError('Песни нет в этом плейлисте')
        self._prepare(headless, volume)
        self._clear_playlist()
        library_player.deactivate()
        self._playlist = {**playlist, 'index': index}
        return self._playlist_track(index)

    def _playlist_track(self, index):
        playlist = self._playlist
        track = playlist['tracks'][index]
        playlist['index'] = index
        state = self._open(track['url'], self._headless, self._volume,
                           save_title=track['title'], save_origin='playlist')
        return state

    def _schedule_playback(self):
        if self._queue_timer:
            self._queue_timer.cancel()
        if not self._monitoring or not self._context:
            return
        def submit():
            try:
                self._worker.submit(self._poll_playback)
            except RuntimeError:
                pass  # The core has shut down the worker.
        self._queue_timer = threading.Timer(1., submit)
        self._queue_timer.daemon = True
        self._queue_timer.start()

    def _poll_playback(self):
        if not self._monitoring or not self._context or not self._page:
            return
        try:
            state = self._status()
            if not self._monitoring or state['state'] == 'error' or state.get('ad'):
                return
            ended = self._ended or self._evaluate_ready('Boolean(window.__vpcPlaybackEnded)')
            if ended:
                if self._playlist:
                    index = self._playlist['index'] + 1
                    if index < len(self._playlist['tracks']):
                        self._playlist_track(index)
                        return
                self._close()
        except Exception:
            # A redirect or a temporary loading document must not discard the
            # user's queue. Next/stop remain usable and the next tick retries.
            logger.debug('[MusicPlayer] playback tick will retry', exc_info=True)
        finally:
            if self._monitoring and self._context:
                self._schedule_playback()

    def _clear_playlist(self):
        # A suspended/completed page must keep blocking late recommendations.
        self._playlist = None
        self._monitoring = False
        if self._queue_timer:
            self._queue_timer.cancel()
            self._queue_timer = None

    def _save_pending(self, row, state):
        if not self._pending_save or not row['media'] or row.get('ad') or state not in ('playing', 'buffering', 'paused'):
            return
        url, fallback, origin = self._pending_save
        expected = (parse_qs(urlsplit(url).query).get('v') or [''])[0]
        actual = (parse_qs(urlsplit(row['url']).query).get('v') or [''])[0]
        if not expected or actual != expected:
            return
        title = re.sub(r'\s*[-–|]\s*YouTube(?: Music)?$', '', row.get('title') or '')
        if title in ('', 'YouTube', 'YouTube Music'):
            if not fallback or fallback == expected:
                return  # Wait for real metadata instead of freezing a generic page title.
            title = fallback
        self._pending_save = None
        try:
            from app.features.music_catalog import save_song
            save_song(url, title, origin=origin, update_only=origin == 'playlist')
        except MusicBrowserError as e:
            self._save_error = str(e)
            logger.warning('[MusicPlayer] song link was not saved: %s', e)

    def _control(self, command, value=None):
        if command == "stop":
            self._close()
            return self._status()
        if not self._page or self._page.is_closed():
            raise MusicBrowserError("Музыкальный браузер не запущен")
        # Read service SPA navigation before resolving next/previous history.
        state = self._status()
        if self._playlist and command in ('next', 'previous'):
            index = self._playlist['index'] + (1 if command == 'next' else -1)
            if not 0 <= index < len(self._playlist['tracks']):
                raise MusicBrowserError('Это последний трек плейлиста' if command == 'next' else 'Это первый трек плейлиста')
            return self._playlist_track(index)
        if command == 'resume':
            self._suspended = False
            self._monitoring = True
            self._schedule_playback()
        if command in ("volume_up", "volume_down"):
            command, value = "volume", round(max(0., min(1., state["volume"] + (.1 if command == "volume_up" else -.1))), 3)
        if command in ("next", "previous") and self._source == "local":
            index = self._local_index + (1 if command == "next" else -1)
            if not 0 <= index < len(self._local_queue):
                raise MusicBrowserError("Это последний MP3 в папке" if command == "next" else "Это первый MP3 в папке")
            return self._local(str(self._local_queue[index]), self._headless, self._volume)
        history_pos = self._history_pos + (1 if command == "next" else -1)
        if command in ("previous", "next") and 0 <= history_pos < len(self._history):
            entry = self._history[history_pos]
            self._replaying = True
            try:
                result = self._local(entry[1], self._headless, self._volume) if entry[0] == "local" else self._open(entry[1], self._headless, self._volume)
                self._history_pos = history_pos
                return result
            finally:
                self._replaying = False
        if command == "volume":
            self._volume = float(value)
        result = self._page.evaluate("""async ({command,value}) => {
            const media=[...document.querySelectorAll('video,audio')];
            const v=media.find(v=>!v.paused) ?? media[0];
            if(!v)return 'На странице пока нет плеера. Откройте видимое окно: возможно, нужен вход или подтверждение.';
            if(command==='pause')v.pause();
            else if(command==='resume'){window.__vpcPlaybackEnded=false;v.muted=false;try{await Promise.race([v.play(),new Promise(r=>setTimeout(r,5000))]);}catch{return 'Браузер не начал воспроизведение. Откройте видимое окно и нажмите Play.';}}
            else if(command==='volume'){window.__vpcMusicVolume=value;v.volume=value;v.muted=value===0;}
            else if(command==='seek'){v.currentTime=Math.max(0,Number.isFinite(v.duration)?Math.min(value,v.duration):value);}
            else if(command==='next'){
                const b=document.querySelector('.next-button.ytmusic-player-bar') || document.querySelector('.ytp-next-button');
                if(!b)return 'На странице нет кнопки следующего трека';
                window.__vpcPlaybackEnded=false;
                b.click();
            }
            else if(command==='previous'){
                const b=document.querySelector('.previous-button.ytmusic-player-bar') || document.querySelector('.ytp-prev-button');
                if(!b || b.disabled || b.getAttribute('aria-disabled')==='true' || b.getBoundingClientRect().width===0)return 'Предыдущего трека пока нет';
                window.__vpcPlaybackEnded=false;
                b.click();
            }
            return '';
        }""", {"command": command, "value": value})
        if result:
            raise MusicBrowserError(result)
        if command in ("next", "previous"):
            # Let the service commit its new player/title before reporting it.
            for _ in range(12):
                self._page.wait_for_timeout(250)
                now = self._status()
                if now['url'] != state['url'] and now['state'] in ('playing', 'buffering'):
                    return now
        return self._status()

    def _remember(self):
        if self._replaying or not self._url:
            return
        target = str(self._local_queue[self._local_index]) if self._source == "local" else self._url
        entry = (self._source, target)
        def key(row):
            return (row[0], (parse_qs(urlsplit(row[1]).query).get('v') or [row[1]])[0])
        if self._history_pos >= 0 and key(self._history[self._history_pos]) == key(entry):
            return
        self._history = self._history[:self._history_pos + 1] + [entry]
        self._history = self._history[-100:]
        self._history_pos = len(self._history) - 1

    def _status(self):
        if not self._context:
            return {"running": False, "state": "stopped", "headless": self._headless,
                    "source": self._source,
                    "title": "", "url": "", "position": 0, "duration": 0,
                    "volume": self._volume, "muted": False, "error": ""}
        if not self._page or self._page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()]
            if pages:
                self._page = pages[0]
            else:
                self._close()
                return self._status()
        row = self._evaluate_ready(STATE_JS)
        self._ended = bool(row['ended'])
        try:
            self._url = youtube_url(row["url"])
        except MusicBrowserError:
            pass  # A consent/login redirect must still reopen the requested link.
        state = ("error" if row["error"] else "waiting" if not row["media"]
                 else "paused" if row["paused"] or row["ended"]
                 else "buffering" if row["ready"] < 3 else "playing")
        if state in ("playing", "paused", "buffering") and row["media"]:
            self._remember()
        self._save_pending(row, state)
        return {"running": True, "state": state, "headless": self._headless,
                "source": self._source,
                "playlist_id": self._playlist['id'] if self._playlist else None,
                "playlist_name": self._playlist['name'] if self._playlist else None,
                "save_error": self._save_error,
                "queue_index": self._playlist['index'] if self._playlist else self._local_index if self._source == 'local' else None,
                "queue_length": len(self._playlist['tracks']) if self._playlist else len(self._local_queue) if self._source == 'local' else None,
                **{k: v for k, v in row.items() if k not in ("media", "paused", "ended", "ready", "url")},
                "url": self._url}

    def _close(self):
        self._clear_playlist()
        self._suspended = False
        self._pending_save = None
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        self._page = None
        self._cdp = None
        try:
            if context:
                context.close()
        finally:
            if playwright:
                playwright.stop()

    async def shutdown(self):
        try:
            await self.execute("stop")
        finally:
            self._worker.shutdown(wait=False, cancel_futures=True)


music_browser = MusicBrowser()

"""Chrome regression for track endings, using only local media and routed pages.

Run: python -m scripts.test_player_endings_live
Uses a temporary profile/catalog; no YouTube requests or personal audio.
"""
import base64
import io
import tempfile
import time
import wave
from pathlib import Path
from unittest.mock import patch

from app.features import music_browser as mb, music_catalog as catalog

FIRST = 'https://www.youtube.com/watch?v=M7lc1UVf-VE'
SECOND = 'https://music.youtube.com/watch?v=0WA2NX4kPSI'


def main():
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\0\0' * 8000 * 5)
    fixture = '''<!doctype html><title>Track</title>
<button class="ytp-autonav-toggle-button" aria-checked="true"
 onclick="this.setAttribute('aria-checked','false')">Autoplay</button>
<button class="ytp-next-button next-button ytmusic-player-bar" onclick="
 history.pushState({},'',location.pathname+'?v=abcdefghijk');
 document.querySelector('audio').currentTime=0;
 document.querySelector('audio').play().catch(()=>{});">Next</button>
<audio controls src="data:audio/wav;base64,AUDIO"></audio><script>
window.siteEnded=0;
document.querySelector('audio').addEventListener('ended',()=>{
 window.siteEnded++;document.querySelector('button.ytp-next-button').click();
});
window.recommend=async()=>{const a=document.querySelector('audio');
 a.currentTime=0;try{await a.play()}catch{};return a.paused;};
</script>'''.replace('AUDIO', base64.b64encode(audio.getvalue()).decode())
    with tempfile.TemporaryDirectory(prefix='vpc-endings-') as folder, \
            patch.object(mb, 'data_dir', return_value=Path(folder)), \
            patch.object(catalog, 'data_dir', return_value=Path(folder)):
        player = mb.MusicBrowser()

        def on_worker(fn):
            return player._worker.submit(fn).result(timeout=40)

        def js(script):
            return on_worker(lambda: player._page.evaluate(script))

        def finish():
            result = js('''async()=>{
                const a=document.querySelector('audio');await a.play();a.currentTime=a.duration-.25;
                for(let i=0;i<150&&!window.__vpcPlaybackEnded;i++)await new Promise(r=>setTimeout(r,20));
                const before={time:a.currentTime,duration:a.duration,paused:a.paused,ready:a.readyState,error:a.error?.message};
                return {ended:window.__vpcPlaybackEnded,siteEnded:window.siteEnded,blocked:await window.recommend(),before};
            }''')
            assert result['ended'] and result['siteEnded'] == 0 and result['blocked'], result

        try:
            def setup():
                player._prepare(True, .01)
                player._context.route('**/*', lambda route: route.fulfill(body=fixture, content_type='text/html'))
            on_worker(setup)
            for url in (FIRST, SECOND):
                if player._context is None:
                    on_worker(setup)
                state = player.execute_sync('open', url=url)
                assert state['state'] == 'playing', state
                assert js("document.querySelector('.ytp-autonav-toggle-button').getAttribute('aria-checked')") == 'false'
                player.execute_sync('control', command='pause')
                state = player.execute_sync('control', command='resume')
                assert state['state'] == 'playing', state
                state = player.execute_sync('control', command='next')
                assert state['state'] == 'playing' and 'abcdefghijk' in state['url'], state
                finish()
                time.sleep(1.5)
                assert not player.execute_sync('status')['running']
                assert player._context is None and player._playlist is None
                print('PASS single track closes its browser; recommendations blocked; manual controls work:', state['source'], flush=True)

            on_worker(setup)
            player.execute_sync('open', url=FIRST)
            js("document.body.classList.add('ad-showing')")
            state = player.execute_sync('status')
            player.execute_sync('control', command='seek', value=state['duration'] - .08)
            on_worker(lambda: player._page.wait_for_function('window.siteEnded===1', timeout=3000))
            assert not js('Boolean(window.__vpcPlaybackEnded)'), 'An ad was mistaken for a completed song'
            print('PASS ad completion can continue to the song', flush=True)

            key = catalog.create_playlist('Fixture playlist')['playlist_id']
            catalog.save_song(FIRST, playlist_id=key)
            catalog.save_song(SECOND, playlist_id=key)
            player.execute_sync('playlist', playlist_id=key)
            finish()
            time.sleep(1.5)  # No UI/status polling: the queue must advance itself.
            state = player.execute_sync('status')
            assert state['queue_index'] == 1 and state['state'] == 'playing', state
            assert state['source'] == 'youtube_music', state
            finish()
            time.sleep(1.5)
            assert player._playlist is None and player._context is None
            assert not player.execute_sync('status')['running']
            print('PASS playlist advances without a UI and fully closes after its last song', flush=True)

            on_worker(setup)
            player.execute_sync('open', url=FIRST)
            player.execute_sync('suspend')
            assert not player._monitoring
            player.execute_sync('close_suspended')
            assert player._context is None
            on_worker(setup)
            player.execute_sync('open', url=FIRST)
            player.execute_sync('suspend')
            player.execute_sync('open', url=SECOND)
            assert player.execute_sync('close_suspended')['running']
            print('PASS file completion closes its suspended browser and cannot close a new song', flush=True)
        finally:
            player.execute_sync('stop')
            player._worker.shutdown(wait=True)


if __name__ == '__main__':
    main()

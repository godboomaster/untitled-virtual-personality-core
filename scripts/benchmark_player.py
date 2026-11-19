"""Compare audible and --mute-audio headless Chrome on the same YouTube clip.

PYTHONPATH=. python -m scripts.benchmark_player --seconds 40 --rounds 3
Only isolated temporary Chrome profiles are used. No personal browser is touched.
CPU: 100% = one logical core. RSS is a sum across Chrome processes, not physical
footprint (shared pages can be counted more than once). No watts/GPU measurement.
"""

import argparse
import functools
import http.server
import json
import platform
import statistics
import time
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psutil
from playwright.sync_api import sync_playwright
from app.features.music_browser import MEDIA_BROWSER_ARGS, MEDIA_IGNORED_DEFAULT_ARGS, configure_headless_page


def snapshot(root):
    values = {}
    for process in [root, *root.children(recursive=True)]:
        try:
            cpu = process.cpu_times()
            values[process.pid] = (cpu.user + cpu.system, process.memory_info().rss)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return values


def summarize(report):
    """Use complete matched pairs only; never let one failed side bias the mean."""
    pairs = {}
    for number in sorted({r['round'] for r in report['runs']}):
        rows = [r for r in report['runs'] if r['round'] == number]
        if (len(rows) == 2 and {r['condition'] for r in rows} == {'sound', 'mute-audio'}
                and all(r['valid'] for r in rows)
                and len({(r['media_after']['width'], r['media_after']['height']) for r in rows}) == 1):
            pairs[number] = rows
    if pairs:
        counts = Counter((rows[0]['media_after']['width'], rows[0]['media_after']['height']) for rows in pairs.values())
        dimensions = counts.most_common(1)[0][0]
        pairs = {n: rows for n, rows in pairs.items()
                 if (rows[0]['media_after']['width'], rows[0]['media_after']['height']) == dimensions}
    report['paired_rounds'] = list(pairs)
    report['excluded_rounds'] = sorted({r['round'] for r in report['runs']} - set(pairs))
    report['comparable'] = bool(pairs)
    report['summary'] = {}
    for condition in ('sound', 'mute-audio'):
        rows = [r for pair in pairs.values() for r in pair if r['condition'] == condition]
        if rows:
            report['summary'][condition] = {key:round(statistics.mean(r[key] for r in rows), 3)
                for key in ('cpu_mean', 'rss_mean_mb', 'startup_sec')}
            report['summary'][condition]['runs'] = len(rows)
            report['summary'][condition]['cpu_run_min'] = min(r['cpu_mean'] for r in rows)
            report['summary'][condition]['cpu_run_max'] = max(r['cpu_mean'] for r in rows)
    return report


MEDIA = """() => {
  const v = document.querySelector('video');
  const p = document.querySelector('#movie_player');
  return {time:v?.currentTime ?? 0, paused:v?.paused ?? true,
    ready:v?.readyState ?? 0, muted:v?.muted, volume:v?.volume,
    width:v?.videoWidth ?? 0, height:v?.videoHeight ?? 0,
    quality:p?.getPlaybackQuality?.(), ad:!!document.querySelector('.ad-showing'),
    error:v?.error?.message ?? document.querySelector('.ytp-error-content-wrap')?.innerText ?? null};
}"""


def run(p, url, muted, seconds, warmup, channel, native_ua=False):
    start = time.monotonic()
    ignored = [arg for arg in MEDIA_IGNORED_DEFAULT_ARGS if not muted or arg != '--mute-audio']
    browser = p.chromium.launch(channel=channel, headless=True,
        ignore_default_args=ignored, args=list(MEDIA_BROWSER_ARGS))
    try:
        cdp = browser.new_browser_cdp_session()
        info = cdp.send('SystemInfo.getProcessInfo')['processInfo']
        root = psutil.Process(next(x['id'] for x in info if x['type'] == 'browser'))
        page = browser.new_page(viewport={'width': 1280, 'height': 720})
        _ua_session = None if native_ua else configure_headless_page(page)
        # Equal nonzero element volume in both modes; only Chrome's flag differs.
        page.add_init_script("for(const name of ['play','volumechange'])document.addEventListener(name, e => { if(e.target instanceof HTMLMediaElement && e.target.volume !== .1) e.target.volume=.1; }, true)")
        page.goto(url, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_selector('video', state='attached', timeout=20000)
        page.locator('video').first.evaluate('v => { v.muted=false; v.volume=.1; return v.play(); }')
        for attempt in range(120):
            ready = page.evaluate(MEDIA)
            if ready['time'] > .2 and ready['ready'] >= 3:
                break
            page.wait_for_timeout(250)
        else:
            raise RuntimeError('Playback did not start')
        startup = time.monotonic() - start
        page.wait_for_timeout(warmup * 1000)
        before_media = page.evaluate(MEDIA)
        previous = snapshot(root)
        previous_at = time.monotonic()
        began = previous_at
        samples = []
        while time.monotonic() - began < seconds:
            page.wait_for_timeout(1000)
            now = time.monotonic()
            processes = snapshot(root)
            cpu_seconds = sum(max(0, cpu - previous.get(pid, (cpu, 0))[0]) for pid, (cpu, rss) in processes.items())
            media = page.evaluate(MEDIA)
            samples.append({'elapsed':round(now-began, 3), 'cpu_percent':round(100*cpu_seconds/(now-previous_at), 3),
                'rss_mb':round(sum(rss for cpu, rss in processes.values())/1024**2, 2),
                'processes':len(processes), 'media':media})
            previous, previous_at = processes, now
        after_media = page.evaluate(MEDIA)
        elapsed = time.monotonic() - began
        valid = (after_media['time'] - before_media['time'] > elapsed * .85
            and all(not x['media']['paused'] and not x['media']['ad'] and not x['media']['error']
                    and not x['media']['muted'] and abs(x['media']['volume']-.1) < .001 for x in samples)
            and len({(x['media']['width'], x['media']['height']) for x in samples}) == 1)
        return {'condition':'mute-audio' if muted else 'sound', 'browser_version':browser.version,
            'native_headless_ua':native_ua,
            'startup_sec':round(startup, 3), 'elapsed_sec':round(elapsed, 3), 'valid':valid,
            'media_before':before_media, 'media_after':after_media,
            'cpu_mean':round(statistics.mean(x['cpu_percent'] for x in samples), 3),
            'rss_mean_mb':round(statistics.mean(x['rss_mb'] for x in samples), 2),
            'samples':samples}
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='https://www.youtube.com/watch?v=M7lc1UVf-VE')
    parser.add_argument('--seconds', type=int, default=40)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--channel', default='chrome')
    parser.add_argument('--native-headless-ua', action='store_true', help='Disable the player\'s YouTube Music UA compatibility adjustment')
    parser.add_argument('--local-media', type=Path, help='Controlled fixture; serve this local audio/video instead of YouTube')
    parser.add_argument('--output', type=Path, default=Path('data/player/benchmarks/latest.json'))
    args = parser.parse_args()
    if not 5 <= args.seconds <= 300 or not 1 <= args.rounds <= 10 or not 0 <= args.warmup <= 60:
        parser.error('seconds: 5..300; rounds: 1..10; warmup: 0..60')
    server = None
    url = args.url
    if args.local_media:
        fixture = args.local_media.resolve()
        if not fixture.is_file():
            parser.error('local-media file does not exist')
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        handler = functools.partial(QuietHandler, directory=str(fixture.parent))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (fixture.parent / 'player-benchmark.html').write_text(f'<video autoplay loop controls src="{fixture.name}"></video>', encoding='utf-8')
        url = f'http://127.0.0.1:{server.server_port}/player-benchmark.html'
    report = {'date_utc':datetime.now(timezone.utc).isoformat(), 'platform':platform.platform(),
        'logical_cores':psutil.cpu_count(), 'ram_gb':round(psutil.virtual_memory().total/1024**3, 1),
        'url':url, 'fixture':str(args.local_media) if args.local_media else None, 'seconds':args.seconds, 'warmup':args.warmup,
        'method':'alternating order; isolated fresh profiles; headless; viewport 1280x720; element volume enforced at 10%; natural playback from start; CPU 100%=one logical core; summed RSS',
        'native_headless_ua':args.native_headless_ua,
        'runs':[]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        for number in range(args.rounds):
            for muted in ([False, True] if number % 2 == 0 else [True, False]):
                print(f'Round {number+1}: {"mute-audio" if muted else "sound"}', flush=True)
                try:
                    result = run(p, url, muted, args.seconds, args.warmup, args.channel, args.native_headless_ua)
                except Exception as e:
                    result = {'condition':'mute-audio' if muted else 'sound', 'valid':False, 'error':str(e)[:1000]}
                result['round'] = number+1
                report['runs'].append(result)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k:v for k,v in result.items() if k not in ('samples', 'media_before', 'media_after')}, ensure_ascii=False), flush=True)
    summarize(report)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'comparable':report['comparable'], 'summary':report['summary'], 'output':str(args.output)}, ensure_ascii=False), flush=True)
    if server:
        server.shutdown()


if __name__ == '__main__':
    main()

"""Opt-in real Chrome/YouTube regression; uses only a temporary guest profile.

PYTHONPATH=. python -m scripts.test_player_live --url URL --seconds 0
--seconds 0 watches a finite track to its end (maximum 15 minutes).
For Music, --seconds 150 --next-seconds 90 also checks the next track.
Sound is intentionally quiet. No sign-in, personal cookies or stream extraction.
"""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from app.features.music_browser import MusicBrowser, youtube_url


async def check(args):
    report = {"url": youtube_url(args.url), "samples": [], "passed": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vpc-media-regression-") as temp, \
            patch("app.features.music_browser.data_dir", return_value=Path(temp)):
        player = MusicBrowser()
        try:
            await player.execute("open", url=args.url, headless=True, volume=.02)
            for _ in range(20):
                status = await player.execute("status")
                if status["state"] == "playing" and status["position"] > .5:
                    break
                if status["state"] == "error":
                    raise AssertionError(status)
                await asyncio.sleep(1)
            else:
                raise AssertionError("Playback did not begin")

            report["identity"] = await asyncio.wrap_future(player._worker.submit(
                lambda: player._page.evaluate("""() => ({ua:navigator.userAgent,
                    uaData:navigator.userAgentData?.toJSON(), webdriver:navigator.webdriver})""")))
            assert report["identity"]["uaData"], "Client Hints were lost"
            assert status["headless"] and not status["muted"]
            paused = await player.execute("control", command="pause")
            assert paused["state"] == "paused", paused
            await player.execute("control", command="volume", value=.02)
            await player.execute("control", command="seek", value=0)
            await player.execute("control", command="resume")

            async def watch(seconds):
                initial = await player.execute("status")
                began = advanced = time.monotonic()
                position = initial["position"]
                duration = initial["duration"]
                limit = seconds or min(900, duration + 30)
                last_print = -1
                while time.monotonic() - began < limit:
                    await asyncio.sleep(2)
                    row = await player.execute("status")
                    elapsed = time.monotonic() - began
                    report["samples"].append({"elapsed": round(elapsed, 2), **row})
                    assert row["running"] and row["headless"], row
                    assert row["state"] != "error", row
                    if row["position"] > position + .2:
                        advanced = time.monotonic()
                    if not seconds and duration > 0:
                        ended = await asyncio.wrap_future(player._worker.submit(
                            lambda: player._page.evaluate("() => document.querySelector('video,audio')?.ended ?? false")))
                        if ended or (row["url"] != initial["url"] and position >= duration - 5):
                            report["completed_track"] = True
                            print("Track completed", flush=True)
                            return row
                    assert time.monotonic() - advanced < 20, f"Playback stalled: {row}"
                    position = row["position"]
                    if int(elapsed // 15) != last_print:
                        last_print = int(elapsed // 15)
                        print(f"{elapsed:.0f}s {row['state']} position={row['position']:.1f}/{row['duration']:.1f}", flush=True)
                if not seconds:
                    raise AssertionError("Track completion was not observed")
                return row

            status = await watch(args.seconds)
            if args.next_seconds:
                old = status["url"]
                await player.execute("control", command="next")
                for _ in range(15):
                    await asyncio.sleep(1)
                    status = await player.execute("status")
                    if status["url"] != old and status["state"] == "playing":
                        break
                else:
                    raise AssertionError(f"Next did not start another track: {status}")
                report["next_title"] = status["title"]
                await watch(args.next_seconds)
            report["passed"] = True
            print("PASS: headless playback, pause, volume, seek, resume" + (", next" if args.next_seconds else ""), flush=True)
        except Exception as e:
            report["failure"] = str(e)
            raise
        finally:
            await player.shutdown()
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--seconds", type=int, default=150)
    parser.add_argument("--next-seconds", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("data/player/benchmarks/live-regression.json"))
    args = parser.parse_args()
    if not 0 <= args.seconds <= 900 or not 0 <= args.next_seconds <= 900:
        parser.error("Duration must be 0..900 seconds")
    asyncio.run(check(args))

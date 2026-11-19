#!/usr/bin/env python3
"""Generate the narrator with the built-in macOS voice. No network or API key."""
import json
from pathlib import Path
import subprocess
import wave

ROOT = Path(__file__).resolve().parent


def main():
    script = json.loads((ROOT / "script.json").read_text())
    audio = ROOT / "audio"
    audio.mkdir(exist_ok=True)
    for i, scene in enumerate(script["scenes"]):
        for j, line in enumerate(scene["lines"]):
            stem = audio / f"{i:02d}-{j:02d}"
            path = stem.with_suffix(".wav")
            if path.exists():
                try:
                    with wave.open(str(path)) as wav:
                        if wav.getnframes() > 1000:
                            print(f"cached {path.name}", flush=True)
                            continue
                except (wave.Error, EOFError):
                    pass
            stem.with_suffix(".txt").write_text(line["say"], encoding="utf-8")
            subprocess.run([
                "say", "-v", script["voice"], "-r", str(script["rate"]),
                "--file-format=WAVE", "--data-format=LEI16@24000",
                "-f", str(stem.with_suffix(".txt")), "-o", str(path),
            ], check=True)
            with wave.open(str(path)) as wav:
                if wav.getnframes() < 1000:
                    raise RuntimeError("Empty speech file: run synthesis outside the sandbox.")
                print(f"{path.name}: {wav.getnframes()/wav.getframerate():.2f}s", flush=True)


if __name__ == "__main__":
    main()

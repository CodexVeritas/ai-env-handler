"""Turn a recording from record.py into the walkthrough video, and optionally a copy small enough for GitHub.

player.html draws each frame with xterm.js in the system's Google Chrome, and ffmpeg encodes the frames. From the repo root:

    uv run --with playwright python scripts/demo/render.py build/demo/recording.json --mp4 build/demo/envh-demo.mp4 --small docs/demo.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

PLAYER = Path(__file__).resolve().parent / "player.html"
WIDTH, HEIGHT = 1920, 1080
FPS = 30
ENCODER_FINISH_SECONDS = 300
# GitHub plays uploaded videos of up to 10 MB inline on free plans; the margin covers the MP4 container.
SMALL_MEGABYTES = 9.5


def mp4_encoder(output: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "png", "-i", "-",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]


def render(page: Page, recording: dict, output: Path) -> None:
    page.goto(PLAYER.as_uri())
    page.wait_for_function("typeof Terminal !== 'undefined'")
    page.evaluate("document.fonts.ready")
    duration = page.evaluate("data => loadDemo(data)", recording)
    frames = int(duration * FPS)
    print(f"rendering {frames} frames ({duration:.1f}s at {FPS} fps)", flush=True)
    encoder = subprocess.Popen(mp4_encoder(output), stdin=subprocess.PIPE)
    try:
        for frame in range(frames):
            page.evaluate("seconds => renderAt(seconds)", frame / FPS)
            encoder.stdin.write(page.screenshot(type="png"))
            if frame % (FPS * 10) == 0:
                print(f"frame {frame}/{frames}", flush=True)
    finally:
        encoder.stdin.close()
        try:
            returncode = encoder.wait(timeout=ENCODER_FINISH_SECONDS)
        except subprocess.TimeoutExpired:
            encoder.kill()
            encoder.wait()
            raise RuntimeError(f"ffmpeg did not finish within {ENCODER_FINISH_SECONDS}s of the last frame") from None
    if returncode != 0:
        raise RuntimeError(f"ffmpeg exited with {returncode}")
    print(f"wrote {output}", flush=True)


def shrink(source: Path, output: Path) -> None:
    """Re-encode in two passes at the bitrate that lands near SMALL_MEGABYTES."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(source)], capture_output=True, text=True, check=True)
    kilobits_per_second = int(SMALL_MEGABYTES * 8000 / float(probe.stdout))
    print(f"shrinking to about {SMALL_MEGABYTES} MB ({kilobits_per_second} kb/s)", flush=True)
    with tempfile.TemporaryDirectory() as work:
        common = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-c:v", "libx264", "-preset", "veryslow", "-b:v", f"{kilobits_per_second}k",
                  "-pix_fmt", "yuv420p", "-passlogfile", str(Path(work) / "pass")]
        subprocess.run([*common, "-pass", "1", "-an", "-f", "mp4", os.devnull], check=True)
        subprocess.run([*common, "-pass", "2", "-movflags", "+faststart", str(output)], check=True)
    print(f"wrote {output} ({output.stat().st_size / 1_000_000:.1f} MB)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the envh walkthrough video")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--mp4", type=Path, required=True, help="the full-quality video")
    parser.add_argument("--small", type=Path, help=f"also write a copy of about {SMALL_MEGABYTES} MB, for uploading to GitHub")
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed")
    recording = json.loads(args.recording.read_text())
    args.mp4.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", args=["--font-render-hinting=none"])
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.on("console", lambda message: print(f"player: {message.text}", flush=True) if message.type in ("error", "warning") else None)
        page.on("pageerror", lambda error: print(f"player error: {error}", file=sys.stderr, flush=True))
        render(page, recording, args.mp4)
        browser.close()
    if args.small:
        args.small.parent.mkdir(parents=True, exist_ok=True)
        shrink(args.mp4, args.small)
    return 0


if __name__ == "__main__":
    sys.exit(main())

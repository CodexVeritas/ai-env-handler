"""Turn a recording from record.py into the demo video (MP4) and the README's GIF.

player.html draws each frame with xterm.js in the system's Google Chrome, and ffmpeg encodes the frames. From the repo root:

    uv run --with playwright python scripts/demo/render.py build/demo/recording.json --mp4 build/demo/envh-demo.mp4 --gif docs/demo.gif
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

PLAYER = Path(__file__).resolve().parent / "player.html"
WIDTH, HEIGHT = 1920, 1080
MP4_FPS = 30
GIF_FPS = 12
GIF_WIDTH = 1440


def mp4_encoder(output: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(MP4_FPS), "-c:v", "png", "-i", "-",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]


def gif_encoder(output: Path) -> list[str]:
    palette = f"scale={GIF_WIDTH}:-1:flags=lanczos,split[frames][copy];[copy]palettegen=max_colors=128:stats_mode=diff[palette];[frames][palette]paletteuse=dither=none:diff_mode=rectangle"
    return ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(GIF_FPS), "-c:v", "png", "-i", "-", "-vf", palette, "-loop", "0", str(output)]


def open_player(page: Page, recording: dict) -> dict:
    page.goto(PLAYER.as_uri())
    page.wait_for_function("typeof Terminal !== 'undefined'")
    page.evaluate("document.fonts.ready")
    return page.evaluate("data => loadDemo(data)", recording)


def render(page: Page, recording: dict, start: float, end: float, fps: int, command: list[str], label: str) -> None:
    open_player(page, recording)
    frames = int((end - start) * fps)
    print(f"{label}: rendering {frames} frames ({end - start:.1f}s at {fps} fps)", flush=True)
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for frame in range(frames):
            page.evaluate("seconds => renderAt(seconds)", start + frame / fps)
            encoder.stdin.write(page.screenshot(type="png"))
            if frame % (fps * 10) == 0:
                print(f"{label}: frame {frame}/{frames}", flush=True)
    finally:
        encoder.stdin.close()
        returncode = encoder.wait()
    if returncode != 0:
        raise RuntimeError(f"{label}: ffmpeg exited with {returncode}")
    print(f"{label}: wrote {command[-1]}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the envh demo video and GIF")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--mp4", type=Path, help="the full video")
    parser.add_argument("--gif", type=Path, help="the README's GIF: the request, approval and run")
    args = parser.parse_args()
    if not (args.mp4 or args.gif):
        parser.error("pass --mp4, --gif or both")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed")
    recording = json.loads(args.recording.read_text())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        page.on("console", lambda message: print(f"player: {message.text}", flush=True) if message.type in ("error", "warning") else None)
        page.on("pageerror", lambda error: print(f"player error: {error}", file=sys.stderr, flush=True))
        timing = open_player(page, recording)
        if args.mp4:
            args.mp4.parent.mkdir(parents=True, exist_ok=True)
            render(page, recording, 0, timing["duration"], MP4_FPS, mp4_encoder(args.mp4), "mp4")
        if args.gif:
            args.gif.parent.mkdir(parents=True, exist_ok=True)
            render(page, recording, timing["gifStart"], timing["gifEnd"], GIF_FPS, gif_encoder(args.gif), "gif")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

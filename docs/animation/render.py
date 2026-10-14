#!/usr/bin/env python3
"""Render kitchen.html into the README GIF and a 1080p MP4.

Usage: render.py [--out DIR] [--fps 12] [--ffmpeg PATH]

Needs Playwright with Chromium (`pip install playwright && playwright install chromium`) and an ffmpeg
with libx264. Writes kitchen.gif (960x540, for the README) and kitchen.mp4 (1920x1080, nearest-neighbour
scaling so the pixels stay sharp) into --out. Copy the GIF to docs/assets/kitchen.gif to publish it.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def frames(folder: Path, fps: float) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 960, "height": 540})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto((HERE / "kitchen.html").as_uri() + "?t=0")
        page.wait_for_selector("body[data-ready='1']", timeout=30000)
        canvas = page.locator("#kitchen")
        for i in range(int(page.evaluate("DURATION") * fps)):
            page.evaluate(f"renderAt({i / fps})")
            canvas.screenshot(path=str(folder / f"f{i:04d}.png"))
        browser.close()
    if errors:
        raise SystemExit("kitchen.html: " + "; ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("."), help="folder for kitchen.gif and kitchen.mp4")
    parser.add_argument("--fps", type=float, default=12)
    parser.add_argument("--ffmpeg", default="ffmpeg", help="an ffmpeg built with libx264")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        frames(folder, args.fps)
        source = ["-framerate", str(args.fps), "-i", str(folder / "f%04d.png")]
        run = lambda *more: subprocess.run([args.ffmpeg, "-y", "-loglevel", "error", *source, *more], check=True)
        run("-vf", "scale=1920:1080:flags=neighbor,fps=24", "-c:v", "libx264", "-crf", "16", "-preset", "slow",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(args.out / "kitchen.mp4"))
        run("-vf", "palettegen=max_colors=128:stats_mode=full", str(folder / "palette.png"))
        run("-i", str(folder / "palette.png"), "-lavfi", "paletteuse=dither=none:diff_mode=rectangle", "-loop", "0",
            str(args.out / "kitchen.gif"))
    print(f"wrote {args.out / 'kitchen.gif'} and {args.out / 'kitchen.mp4'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

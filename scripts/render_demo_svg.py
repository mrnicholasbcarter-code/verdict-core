#!/usr/bin/env python3
"""Render an asciicast to an animated SVG and a static poster of the last frame.

The animated SVG uses ``--no-cursor``. The poster is ``svg-term --at <last>``
with no animation. Both come from the same cast.

    python scripts/render_demo_svg.py docs/assets/demo-tui.cast \
        docs/assets/demo-tui.svg docs/assets/demo-tui-poster.svg
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

WIDTH, HEIGHT = 110, 34


def last_stamp_ms(cast: Path) -> int:
    """Millisecond timestamp of the last output event."""
    lines = cast.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise SystemExit(f"{cast} has no events")
    last = 0.0
    for line in lines[1:]:
        if not line.strip():
            continue
        event = json.loads(line)
        if isinstance(event, list) and event and isinstance(event[0], (int, float)):
            last = float(event[0])
    return max(0, round(last * 1000))


def _svg_term(cast: Path, out: Path, extra: list[str]) -> None:
    header = json.loads(cast.read_text(encoding="utf-8").splitlines()[0])
    width, height = int(header["width"]), int(header["height"])
    if not (1 <= width <= 300 and 1 <= height <= 150):
        raise SystemExit("cast dimensions are outside the bounded rendering range")
    cmd = [
        "npx",
        "-y",
        "svg-term-cli@2.1.1",
        "--in",
        str(cast),
        "--out",
        str(out),
        "--window",
        "--no-cursor",
        "--width",
        str(width),
        "--height",
        str(height),
        *extra,
    ]
    subprocess.run(cmd, check=True)
    # Recordings use standard Unicode, not icon fonts. Keep a portable fallback
    # and do not suggest a locally installed Powerline/Nerd font is required.
    svg = out.read_text(encoding="utf-8")
    out.write_text(svg.replace(",'Powerline Symbols'", ""), encoding="utf-8")


def render(cast: Path, animated: Path, poster: Path) -> None:
    if shutil.which("npx") is None:
        raise SystemExit("npx is required to render the demo SVG")
    at_ms = last_stamp_ms(cast)
    animated.parent.mkdir(parents=True, exist_ok=True)
    _svg_term(cast, animated, [])
    _svg_term(cast, poster, ["--at", str(at_ms)])
    text = poster.read_text(encoding="utf-8")
    if "<animate" in text or "@keyframes" in text:
        raise SystemExit(f"{poster} is not a static frame")
    print(f"rendered {animated.name} and {poster.name} at {at_ms} ms", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("cast", type=Path)
    parser.add_argument("animated", type=Path)
    parser.add_argument("poster", type=Path)
    args = parser.parse_args(argv)
    render(args.cast, args.animated, args.poster)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

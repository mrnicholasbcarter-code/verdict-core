#!/usr/bin/env python3
"""Render an asciicast to an animated SVG and a static poster of the COMPLETE cockpit frame.

The animated SVG uses ``--no-cursor``. The poster uses ``svg-term --at`` at the
chosen COMPLETE redraw, cropped to its content height. Both use the same cast.
Use ``--poster-only`` to refresh the poster without changing the animation.

    python scripts/render_demo_svg.py docs/assets/demo-tui.cast \
        docs/assets/demo-tui.svg docs/assets/demo-tui-poster.svg
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

WIDTH, HEIGHT = 110, 34


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b.")


def _visible(data: str) -> str:
    return _ANSI_RE.sub("", data)


def poster_frame(cast: Path) -> tuple[int, int]:
    """Compressed-time ms and content height of the first COMPLETE cockpit redraw.

    svg-term's --at flag uses the *compressed* timeline (idle_time_limit applied),
    not the raw recording timestamps.  We simulate the same compression so the
    returned value matches what svg-term expects.

    Falls back to the last compressed event time and header height without COMPLETE.
    The poster is the README reduced-motion fallback: all nodes VALIDATED,
    failure/reassign history visible, review PASS.

    The cockpit is identified by its box-drawing border (\u2503 / \u2501 chars).
    Plain receipt text also contains "COMPLETE" and "VALIDATED" but has no box
    chars, so requiring them distinguishes the two contexts.
    """
    lines = cast.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise SystemExit(f"{cast} has no events")
    header = json.loads(lines[0])
    idle_limit = float(header.get("idle_time_limit", 1e9))
    prev_raw = 0.0
    compressed = 0.0
    for line in lines[1:]:
        if not line.strip():
            continue
        event = json.loads(line)
        if not (isinstance(event, list) and len(event) == 3):
            continue
        at, kind, data = event
        if not isinstance(at, (int, float)):
            continue
        at = float(at)
        compressed += min(at - prev_raw, idle_limit)
        prev_raw = at
        if kind != "o":
            continue
        text = _visible(data)
        # Require box-drawing chars to distinguish TUI cockpit from plain
        # receipt text, which also contains "COMPLETE" and "VALIDATED".
        if (
            "COMPLETE" in text
            and ("VALIDATED" in text or "REASSIGN" in text.upper())
            and ("\u2503" in data or "\u2501" in data)  # \u2503=┃  \u2501=━
        ):
            # The recorder emits full cockpit redraws. Split only on LF: the
            # leading CR in Rich's cursor reset is not an extra terminal row.
            rows = text.split("\n")
            height = max(index + 1 for index, row in enumerate(rows) if row.strip())
            return max(0, round(compressed * 1000)), min(height, int(header["height"]))
    return max(0, round(compressed * 1000)), int(header["height"])


def poster_stamp_ms(cast: Path) -> int:
    """Compressed-time position of the chosen poster frame."""
    return poster_frame(cast)[0]


def _svg_term(cast: Path, out: Path, extra: list[str], *, height: int | None = None) -> None:
    header = json.loads(cast.read_text(encoding="utf-8").splitlines()[0])
    width = int(header["width"])
    height = int(header["height"]) if height is None else height
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


def render(cast: Path, animated: Path, poster: Path, *, poster_only: bool = False) -> None:
    if shutil.which("npx") is None:
        raise SystemExit("npx is required to render the demo SVG")
    at_ms, height = poster_frame(cast)
    poster.parent.mkdir(parents=True, exist_ok=True)
    if not poster_only:
        animated.parent.mkdir(parents=True, exist_ok=True)
        _svg_term(cast, animated, [])
    _svg_term(cast, poster, ["--at", str(at_ms)], height=height)
    text = poster.read_text(encoding="utf-8")
    if "<animate" in text or "@keyframes" in text:
        raise SystemExit(f"{poster} is not a static frame")
    print(f"rendered {poster.name} at {at_ms} ms ({height} rows)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("cast", type=Path)
    parser.add_argument("animated", type=Path)
    parser.add_argument("poster", type=Path)
    parser.add_argument(
        "--poster-only", action="store_true", help="Leave the animated SVG unchanged"
    )
    args = parser.parse_args(argv)
    render(args.cast, args.animated, args.poster, poster_only=args.poster_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

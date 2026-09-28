#!/usr/bin/env python3
"""Record TUI replay as asciinema v2 cast for visual demo.

Uses the same stdlib PTY approach as record_demo.py. Records `verdict watch --replay`
with timing driven by the event stream timestamps.

Usage:
    python scripts/record_tui_demo.py /tmp/dogfood-runs/df83/20260928T110700Z
    python scripts/record_tui_demo.py --speed 2.0 /path/to/run

Then render SVG:
    npx -y svg-term-cli@2.1.1 --in docs/assets/demo-tui.cast \
        --out docs/assets/demo-tui.svg --window --width 110 --height 34
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 110, 34


def _run_in_pty(argv: list[str], env: dict[str, str]) -> tuple[bytes, int]:
    """Run command in PTY and capture output. Same pattern as record_demo.py."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.chdir(ROOT)
        os.execvpe(argv[0], argv, env)
    chunks: list[bytes] = []
    while True:
        ready, _, _ = select.select([fd], [], [], 30)
        if not ready:
            break
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return b"".join(chunks), os.waitstatus_to_exitcode(status)


def record_replay(run_dir: Path, speed: float = 1.0, output: Path | None = None) -> None:
    """Record verdict watch --replay in a PTY."""
    if output is None:
        output = ROOT / "docs" / "assets" / "demo-tui.cast"

    output.parent.mkdir(parents=True, exist_ok=True)

    if not run_dir.exists():
        print(f"Run directory not found: {run_dir}", file=sys.stderr)
        sys.exit(1)

    events_file = run_dir / "events.jsonl"
    if not events_file.exists():
        print(f"No events.jsonl in {run_dir}", file=sys.stderr)
        sys.exit(1)

    # Build command
    python = sys.executable
    argv = [
        python,
        "-m",
        "verdict.orchestration.cli",
        "watch",
        str(run_dir),
        "--replay",
        "--speed",
        str(speed),
    ]

    # Environment - NO_COLOR forces plain text output we can capture
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "TERM": "xterm-256color",
        "NO_COLOR": "1",
        "COLUMNS": str(WIDTH),
        "LINES": str(HEIGHT),
    }

    print(f"Recording replay at {speed}x speed...", file=sys.stderr)
    raw_output, exit_code = _run_in_pty(argv, env)
    text = raw_output.decode("utf-8", errors="replace")

    if exit_code != 0:
        print(f"warning: command exited with code {exit_code}", file=sys.stderr)

    # Write asciinema v2 format with paced output
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "title": f"Verdict Orchestration TUI Replay ({run_dir.name})",
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
    }

    # Pace the output by lines
    events = []
    clock = 0.0
    line_delay = 0.08  # seconds per line

    for line in text.splitlines(keepends=True):
        events.append([round(clock, 3), "o", line])
        clock += line_delay

    with output.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for event in events:
            f.write(json.dumps(event) + "\n")

    print(f"Recorded {len(events)} events to {output}", file=sys.stderr)
    print(f"Duration: {clock:.1f}s", file=sys.stderr)
    print("\nRender with:", file=sys.stderr)
    print(
        f"  npx -y svg-term-cli@2.1.1 --in {output} --out {output.with_suffix('.svg')} --window --width {WIDTH} --height {HEIGHT}",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("run_dir", type=Path, help="Path to completed run directory")
    parser.add_argument(
        "--speed", type=float, default=1.0, help="Replay speed multiplier (default: 1.0)"
    )
    parser.add_argument(
        "--output", type=Path, help="Output .cast file (default: docs/assets/demo-tui.cast)"
    )

    args = parser.parse_args()
    record_replay(args.run_dir, speed=args.speed, output=args.output)


if __name__ == "__main__":
    main()

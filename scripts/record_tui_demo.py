#!/usr/bin/env python3
"""Record TUI replay as asciinema v2 cast for visual demo.

Uses the same stdlib PTY approach as record_demo.py. Records `verdict watch --replay`
with timing driven by the event stream timestamps (same scaling and gap cap as follow_replay).

Usage (from verdict-core root):
    python scripts/record_tui_demo.py docs/proof/live-controller-run
    python scripts/record_tui_demo.py --speed 0.5 docs/proof/live-controller-run

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


def _read_event_timestamps(events_file: Path) -> list[float]:
    """Read event timestamps and compute inter-event delays."""
    import datetime as dt

    timestamps: list[float] = []
    with events_file.open("r") as f:
        for line in f:
            if not line.strip():
                continue
            event = json.loads(line)
            at = event.get("at", "")
            if at:
                # Parse ISO timestamp
                try:
                    ts = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
                    timestamps.append(ts.timestamp())
                except Exception:
                    # If parsing fails, use 0
                    timestamps.append(0.0)
            else:
                timestamps.append(0.0)

    return timestamps


def record_replay(
    run_dir: Path, speed: float = 1.0, max_gap: float = 1.5, output: Path | None = None
) -> None:
    """Record verdict watch --replay in a PTY with event-based pacing."""
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

    # Build command - use rich output (no NO_COLOR) so we get the colored TUI
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

    # Environment - TERM for rich output
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "TERM": "xterm-256color",
        "COLUMNS": str(WIDTH),
        "LINES": str(HEIGHT),
    }

    print(f"Recording replay at {speed}x speed...", file=sys.stderr)
    raw_output, exit_code = _run_in_pty(argv, env)
    text = raw_output.decode("utf-8", errors="replace")

    if exit_code != 0:
        print(f"warning: command exited with code {exit_code}", file=sys.stderr)

    # Read event timestamps to compute pacing
    timestamps = _read_event_timestamps(events_file)

    # Compute delays between events (with max_gap cap and speed scaling)
    delays: list[float] = []
    for i in range(len(timestamps) - 1):
        if timestamps[i] > 0 and timestamps[i + 1] > 0:
            delay = timestamps[i + 1] - timestamps[i]
            delay = min(delay, max_gap) / speed
            delays.append(max(0.0, delay))
        else:
            delays.append(0.0)

    # Write asciinema v2 format with event-based pacing
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "title": f"Verdict Orchestration TUI Replay ({run_dir.name})",
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
    }

    # Split text into lines and assign delays based on event boundaries
    # We approximate: distribute delays across output lines proportionally
    lines = text.splitlines(keepends=True)

    # Simple approach: assign delays evenly across lines
    # More sophisticated: detect event markers in output and sync
    # For now, use a simple heuristic: spread event delays across output lines
    events = []
    clock = 0.0

    if not delays:
        # Fallback: fixed timing if we couldn't parse timestamps
        line_delay = 0.08
        for line in lines:
            events.append([round(clock, 3), "o", line])
            clock += line_delay
    else:
        # Distribute event delays across lines
        # Assume roughly proportional output per event
        lines_per_event = len(lines) / (len(delays) + 1) if delays else 1
        delay_idx = 0
        lines_since_event = 0

        for line in lines:
            events.append([round(clock, 3), "o", line])

            # Advance clock based on event timing
            lines_since_event += 1
            if delay_idx < len(delays) and lines_since_event >= lines_per_event:
                clock += delays[delay_idx]
                delay_idx += 1
                lines_since_event = 0
            else:
                # Small inter-line delay for readability
                clock += 0.02

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
    parser.add_argument(
        "run_dir",
        type=Path,
        help="Path to completed run directory (e.g., docs/proof/live-controller-run)",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0, help="Replay speed multiplier (default: 1.0)"
    )
    parser.add_argument(
        "--max-gap",
        type=float,
        default=1.5,
        help="Maximum delay between events in seconds (default: 1.5)",
    )
    parser.add_argument(
        "--output", type=Path, help="Output .cast file (default: docs/assets/demo-tui.cast)"
    )

    args = parser.parse_args()
    record_replay(args.run_dir, speed=args.speed, max_gap=args.max_gap, output=args.output)


if __name__ == "__main__":
    main()

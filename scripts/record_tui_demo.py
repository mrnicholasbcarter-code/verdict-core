#!/usr/bin/env python3
"""Record TUI replay as an asciinema v2 cast.

Captures the real PTY output of ``verdict watch <run> --replay``. Each read
from the PTY is one asciicast event, timestamped when the bytes arrived.
Escape sequences and synchronized-output blocks are never split or merged.

The child PTY is sized to the cast header (TIOCSWINSZ plus COLUMNS/LINES).

A capture that fails validation is not written. The process exits non-zero
and prints the reason.

    python scripts/record_tui_demo.py docs/proof/live-controller-run
    python scripts/record_tui_demo.py --output docs/assets/demo-tui-direct.cast \
        docs/proof/harness-independence-2026-09-28/direct-gateway-run

Then render SVG:

    npx -y svg-term-cli@2.1.1 --in docs/assets/demo-tui.cast \
        --out docs/assets/demo-tui.svg --window --width 110 --height 34
"""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 110, 34
# Rich Live draws the final frame, stops, then the integrity line is printed.
COMPLETION_MARKER = "integrity: OK (events digest verified)"
MIN_EVENTS = 2
MIN_BYTES = 200
READ_TIMEOUT = 30.0

# CSI / OSC / DCS that must not be cut between two output events.
_INTRODUCERS = (b"\x1b[", b"\x1b]", b"\x1bP")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b.")


def visible_text(data: bytes) -> str:
    """Decode and drop ANSI sequences so a marker can be found inside styled output."""
    text = data.decode("utf-8", errors="replace")
    return _ANSI.sub("", text)


class CaptureError(RuntimeError):
    """A recording must not be written."""


def set_pty_size(fd: int, rows: int, cols: int) -> None:
    """Set the child window to the cast dimensions (TIOCSWINSZ)."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl_ioctl(fd, termios.TIOCSWINSZ, winsize)


def fcntl_ioctl(fd: int, op: int, arg: bytes) -> None:
    import fcntl

    fcntl.ioctl(fd, op, arg)


def read_pty_events(
    argv: list[str],
    env: dict[str, str],
    *,
    rows: int,
    cols: int,
    read_timeout: float = READ_TIMEOUT,
) -> tuple[list[tuple[float, bytes]], int]:
    """Run argv in a sized PTY. Return (monotonic-offset, bytes) per read plus exit code."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.chdir(ROOT)
        os.execvpe(argv[0], argv, env)
    set_pty_size(fd, rows, cols)
    chunks: list[tuple[float, bytes]] = []
    start = time.monotonic()
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], read_timeout)
            if not ready:
                break
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append((time.monotonic() - start, data))
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    return chunks, os.waitstatus_to_exitcode(status)


def _sync_depth_delta(data: bytes) -> int:
    """Net synchronized-output depth change (DECSET 2026 on, DECRST 2026 off)."""
    return data.count(b"\x1b[?2026h") - data.count(b"\x1b[?2026l")


def _sync_still_open(data: bytes) -> bool:
    """True while a DEC 2026 synchronized-output block has not been closed."""
    return _sync_depth_delta(data) > 0


def _ends_inside_escape(data: bytes) -> bool:
    """True when the chunk ends mid CSI, OSC, or DCS (no terminator yet)."""
    last = -1
    for intro in _INTRODUCERS:
        pos = data.rfind(intro)
        if pos > last:
            last = pos
    if last < 0:
        return data.endswith(b"\x1b")
    tail = data[last:]
    if tail.startswith(b"\x1b["):
        # CSI ends at a final byte 0x40-0x7E.
        return not any(0x40 <= b <= 0x7E for b in tail[2:])
    # OSC ends at BEL or ST; DCS ends at ST.
    return b"\x07" not in tail[2:] and b"\x1b\\" not in tail[2:]


def coalesce_atomic(chunks: list[tuple[float, bytes]]) -> list[tuple[float, bytes]]:
    """Join only reads that split one escape or one synchronized-output block.

    Timing of a joined event is the timestamp of its first read. Complete
    writes are never merged with the next write.
    """
    if not chunks:
        return []
    out: list[tuple[float, bytes]] = []
    start_at, buf = chunks[0]
    for at, data in chunks[1:]:
        if _sync_still_open(buf) or _ends_inside_escape(buf):
            buf += data
            continue
        out.append((start_at, buf))
        start_at, buf = at, data
    out.append((start_at, buf))
    return out


def validate_capture(
    chunks: list[tuple[float, bytes]],
    exit_code: int,
    *,
    marker: str = COMPLETION_MARKER,
    min_bytes: int = MIN_BYTES,
    min_events: int = MIN_EVENTS,
) -> None:
    """Reject a capture that is not a faithful completed replay. Raises CaptureError."""
    if exit_code != 0:
        raise CaptureError(f"command exited {exit_code}")
    raw = b"".join(data for _, data in chunks)
    if len(raw) < min_bytes:
        raise CaptureError(f"capture too short ({len(raw)} bytes)")
    if len(chunks) < min_events:
        raise CaptureError(f"capture has {len(chunks)} frames; need at least {min_events}")
    text = visible_text(raw)
    if "Traceback (most recent call last)" in text:
        raise CaptureError("traceback in capture")
    if marker not in text:
        raise CaptureError(f"capture never reaches completion marker {marker!r}")
    last = visible_text(chunks[-1][1])
    if marker not in last:
        raise CaptureError("last frame lacks the completion marker")


def to_asciicast(chunks: list[tuple[float, bytes]], *, width: int, height: int, title: str) -> str:
    """Asciicast v2 document. Timestamps are real read times, rounded to milliseconds."""
    header = {
        "version": 2,
        "width": width,
        "height": height,
        "title": title,
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
    }
    lines = [json.dumps(header)]
    last = 0.0
    for at, data in chunks:
        stamp = max(last, round(at, 3))
        last = stamp
        lines.append(json.dumps([stamp, "o", data.decode("utf-8", errors="replace")]))
    return "\n".join(lines) + "\n"


def record_replay(
    run_dir: Path,
    speed: float = 1.0,
    output: Path | None = None,
    *,
    rows: int = HEIGHT,
    cols: int = WIDTH,
    marker: str = COMPLETION_MARKER,
) -> None:
    """Record ``verdict watch --replay`` and write the cast only if it validates."""
    if output is None:
        output = ROOT / "docs" / "assets" / "demo-tui.cast"
    if not run_dir.exists():
        raise CaptureError(f"run directory not found: {run_dir}")
    if not (run_dir / "events.jsonl").exists():
        raise CaptureError(f"no events.jsonl in {run_dir}")

    argv = [
        sys.executable,
        "-m",
        "verdict.orchestration.cli",
        "watch",
        str(run_dir),
        "--replay",
        "--speed",
        str(speed),
    ]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT),
        "TERM": "xterm-256color",
        "COLUMNS": str(cols),
        "LINES": str(rows),
        "PYTHONUNBUFFERED": "1",
    }
    print(f"recording {run_dir} at {speed}x into a {cols}x{rows} pty", file=sys.stderr)
    chunks, exit_code = read_pty_events(argv, env, rows=rows, cols=cols)
    atomic = coalesce_atomic(chunks)
    validate_capture(atomic, exit_code, marker=marker)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = to_asciicast(
        atomic, width=cols, height=rows, title=f"Verdict Orchestration TUI Replay ({run_dir.name})"
    )
    output.write_text(body, encoding="utf-8")
    duration = atomic[-1][0] if atomic else 0.0
    print(f"wrote {output} ({len(atomic)} frames, {duration:.1f}s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("run_dir", type=Path, help="Completed run directory to replay")
    parser.add_argument("--speed", type=float, default=8.0, help="Replay speed (default 8)")
    parser.add_argument(
        "--output", type=Path, help="Output .cast (default docs/assets/demo-tui.cast)"
    )
    args = parser.parse_args(argv)
    try:
        record_replay(args.run_dir, speed=args.speed, output=args.output)
    except CaptureError as exc:
        print(f"capture rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Record TUI replay as an asciinema v2 cast.

Captures the real PTY output of ``verdict watch <run> --replay``. Each read
from the PTY is one asciicast event, timestamped when the bytes arrived.
Escape sequences and synchronized-output blocks are never split or merged.

The child PTY is sized to the cast header (TIOCSWINSZ plus COLUMNS/LINES).

A capture that fails validation is not written. The process exits non-zero
and prints the reason.

    python scripts/record_tui_demo.py --scenario --speed 1
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
import signal
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
                os.kill(pid, signal.SIGKILL)
                raise CaptureError(f"PTY produced no output for {read_timeout:g}s")
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


def trim_leading_idle(chunks: list[tuple[float, bytes]]) -> tuple[list[tuple[float, bytes]], float]:
    """Drop only the idle gap before the first output event.

    Event order and the gaps between events stay as recorded. The first event
    moves to time 0, and every later timestamp shifts by the same amount.
    """
    if not chunks:
        return [], 0.0
    offset = chunks[0][0]
    return [(at - offset, data) for at, data in chunks], offset


def to_asciicast(
    chunks: list[tuple[float, bytes]],
    *,
    width: int,
    height: int,
    title: str,
    idle_trimmed: float = 0.0,
) -> str:
    """Asciicast v2 document. Timestamps are real read times, rounded to milliseconds.

    ``idle_time_limit`` records seconds removed before the first output event.
    """
    header = {
        "version": 2,
        "width": width,
        "height": height,
        "title": title,
        "idle_time_limit": round(idle_trimmed, 3),
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
    atomic, idle_trimmed = trim_leading_idle(atomic)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = to_asciicast(
        atomic,
        width=cols,
        height=rows,
        title=(
            f"Verdict Orchestration TUI Replay ({run_dir.name}), "
            f"replayed at {speed:g}x real time; idle gaps longer than 1.5s were shortened"
        ),
        idle_trimmed=idle_trimmed,
    )
    output.write_text(body, encoding="utf-8")
    duration = atomic[-1][0] if atomic else 0.0
    print(f"wrote {output} ({len(atomic)} frames, {duration:.1f}s)", file=sys.stderr)


SCENARIO_LABEL = "offline scenario, scripted workers, injected faults"
SCENARIO_HEIGHT = 72  # Full home (65 rows) and cockpit; never rely on Live cropping.


def validate_scenario_events(events: list[dict[str, object]]) -> None:
    """Reject demos that cannot prove concurrent workers and ordered recovery."""
    active: set[str] = set()
    concurrent = False
    milestones: list[str] = []
    for event in events:
        kind = event.get("type")
        node = str(event.get("node_id") or "")
        if kind == "dispatch":
            active.add(node)
            concurrent = concurrent or len(active) >= 2
        elif kind == "terminal":
            active.discard(node)
        if kind in ("failure", "cooldown", "reassign"):
            milestones.append(str(kind))
    if not concurrent:
        raise CaptureError("scenario never has two dispatched workers running together")
    wanted = iter(("failure", "cooldown", "reassign"))
    next_kind = next(wanted)
    for kind in milestones:
        if kind == next_kind:
            next_kind = next(wanted, "")
    if next_kind:
        raise CaptureError("scenario lacks ordered failure -> cooldown -> reassign evidence")


def validate_scenario_height(events: list[dict[str, object]], *, include_home: bool) -> None:
    """Reject oversized source frames before Rich Live can crop their bottom border."""
    import io

    from rich.console import Console

    from verdict.home import HomeState, render_home
    from verdict.orchestration.tui import RunView, render

    if len(events) > 2000:
        raise CaptureError("scenario exceeds the bounded 2000-frame capture limit")
    console = Console(
        file=io.StringIO(), width=WIDTH, force_terminal=True, color_system="truecolor"
    )
    if include_home:
        home_rows = len(
            console.render_lines(
                render_home(HomeState(), plain=False, width=WIDTH, interactive=True)
            )
        )
        if home_rows + 2 > SCENARIO_HEIGHT:
            raise CaptureError(f"home frame would be clipped: {home_rows} rows")
    view = RunView()
    for event in events:
        view.apply(event)
        count = len(console.render_lines(render(view, width=WIDTH)))
        # Replay integrity line and final newline also need terminal space.
        if count + 2 > SCENARIO_HEIGHT:
            raise CaptureError(
                f"cockpit frame would be clipped: {count} rows at seq {event.get('seq')}"
            )


def scenario_session(speed: float, *, short: bool = False) -> None:
    """Sequence the same entry points used by the CLI, inside the captured PTY.

    Only this recording driver adds the home/receipt transitions. Scenario events,
    cockpit timing and receipt text come from the production commands unchanged.
    """
    import shutil
    import subprocess
    import tempfile

    from rich.console import Console

    from verdict.home import HomeState, render_home
    from verdict.orchestration.demo_scenario import run_flagship_scenario
    from verdict.orchestration.tui import follow_replay

    console = Console()
    with tempfile.TemporaryDirectory(prefix="verdict-record-scenario-") as temporary:
        root = Path(temporary)
        if not short:
            console.print(
                render_home(HomeState(), plain=False, width=console.width, interactive=True)
            )
            console.print(SCENARIO_LABEL, markup=False)
            # A real, recorded reading pause; no cast timestamps are synthesized.
            time.sleep(2)
        scenario = run_flagship_scenario(
            root / "runs", workspace_root=root / "workspace", worker_seconds=1.5
        )
        validate_scenario_events(scenario.events)
        validate_scenario_height(scenario.events, include_home=not short)
        console.clear()
        follow_replay(scenario.run_dir / "events.jsonl", console=console, speed=speed)
        time.sleep(2)
        receipt = subprocess.run(
            [sys.executable, "-m", "verdict", "run-receipt", str(scenario.run_dir)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        tampered = root / "tampered"
        shutil.copytree(scenario.run_dir, tampered)
        tamper_events(tampered / "events.jsonl")
        rejected = subprocess.run(
            [sys.executable, "-m", "verdict", "run-receipt", str(tampered)],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if rejected.returncode != 1 or b"events_digest mismatch" not in rejected.stdout:
            raise CaptureError("tampered receipt was not rejected with digest mismatch")
        console.clear()
        console.print(SCENARIO_LABEL, markup=False)
        # Print real CLI output with observed exit statuses (not speculative annotations).
        ok_status = f"exit status: {receipt.returncode}\n".encode()
        fail_status = f"exit status: {rejected.returncode}\n".encode()
        sys.stdout.buffer.write(
            b"$ verdict run-receipt <temporary offline run>\n"
            + receipt.stdout
            + ok_status
            + b"\n$ verdict run-receipt <copy with one event byte changed>\n"
            + rejected.stdout
        )
        sys.stdout.buffer.flush()
        # Short pause then a sentinel line to ensure the PTY reader sees
        # the full rejected output before the process exits.
        time.sleep(0.05)
        sys.stdout.buffer.write(fail_status)
        sys.stdout.buffer.flush()
        time.sleep(2)


def tamper_events(path: Path) -> None:
    """Change exactly one goal byte while keeping the copied event log valid JSON."""
    raw = path.read_bytes()
    if b"flagship" not in raw:
        raise CaptureError("scenario event log has no flagship goal to tamper")
    path.write_bytes(raw.replace(b"flagship", b"Flagship", 1))


def record_scenario(speed: float = 1.0, output: Path | None = None, *, short: bool = False) -> None:
    """Capture the real offline demo scenario; reject incomplete/failed runs."""
    import tempfile

    if speed <= 0:
        raise CaptureError("speed must be positive")
    output = output or ROOT / "docs" / "assets" / "demo-tui.cast"
    with tempfile.TemporaryDirectory(prefix="verdict-record-home-") as home:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "PYTHONPATH": str(ROOT),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": str(WIDTH),
            "LINES": str(SCENARIO_HEIGHT),
            "PYTHONUNBUFFERED": "1",
        }
        chunks, exit_code = read_pty_events(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--scenario-session",
                "--speed",
                str(speed),
            ]
            + (["--short"] if short else []),
            env,
            rows=SCENARIO_HEIGHT,
            cols=WIDTH,
        )
    atomic = coalesce_atomic(chunks)
    validate_capture(atomic, exit_code, marker="exit status: 1")
    text = visible_text(b"".join(data for _, data in atomic))
    for required in ("COMPLETE", "cooldown", "reassign", COMPLETION_MARKER):
        if required.casefold() not in text.casefold():
            raise CaptureError(f"scenario capture lacks {required!r}")
    atomic, idle_trimmed = trim_leading_idle(atomic)
    body = to_asciicast(
        atomic,
        width=WIDTH,
        height=SCENARIO_HEIGHT,
        title=(
            f"Verdict: {SCENARIO_LABEL}; replay speed {speed:g}x; "
            "recorded from scripts/record_tui_demo.py --scenario; "
            "real PTY read times, replay gaps over 1.5s capped"
        ),
        idle_trimmed=idle_trimmed,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    print(f"wrote {output} ({len(atomic)} frames, {atomic[-1][0]:.1f}s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("run_dir", type=Path, nargs="?", help="Completed run directory to replay")
    parser.add_argument(
        "--scenario", action="store_true", help="Record the offline verdict demo scenario"
    )
    parser.add_argument("--scenario-session", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--short", action="store_true", help="Omit home from the scenario recording"
    )
    parser.add_argument("--speed", type=float, default=1.0, help="Replay speed (default 1)")
    parser.add_argument(
        "--output", type=Path, help="Output .cast (default docs/assets/demo-tui.cast)"
    )
    args = parser.parse_args(argv)
    try:
        if args.scenario_session:
            scenario_session(args.speed, short=args.short)
        elif args.scenario:
            record_scenario(speed=args.speed, output=args.output, short=args.short)
        elif args.run_dir is not None:
            record_replay(args.run_dir, speed=args.speed, output=args.output)
        else:
            parser.error("provide a run directory or --scenario")
    except CaptureError as exc:
        print(f"capture rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

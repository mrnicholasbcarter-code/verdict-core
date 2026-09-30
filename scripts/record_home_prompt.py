#!/usr/bin/env python3
"""Record the home command prompt demo as an asciinema v2 cast."""

from __future__ import annotations

import contextlib
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 110, 40


def set_pty_size(fd: int, rows: int, cols: int) -> None:
    import fcntl

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def read_pty_with_input(
    argv: list[str],
    env: dict[str, str],
    inputs: list[tuple[float, str]],
    *,
    rows: int = HEIGHT,
    cols: int = WIDTH,
    timeout: float = 60.0,
) -> list[tuple[float, bytes]]:
    """Run argv in a PTY, sending scripted inputs at specified delays."""
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        set_pty_size(sys.stdout.fileno(), rows, cols)
        os.environ.clear()
        os.environ.update(env)
        os.execvp(argv[0], argv)

    set_pty_size(master_fd, rows, cols)
    chunks: list[tuple[float, bytes]] = []
    start = time.monotonic()
    input_idx = 0

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                break

            # Send pending inputs
            while input_idx < len(inputs):
                if elapsed >= inputs[input_idx][0]:
                    os.write(master_fd, inputs[input_idx][1].encode())
                    input_idx += 1
                else:
                    break

            # Select timeout
            if input_idx < len(inputs):
                wait = max(0.01, min(inputs[input_idx][0] - elapsed, 0.5))
            else:
                wait = 0.5

            ready, _, _ = select.select([master_fd], [], [], wait)
            if ready:
                try:
                    data = os.read(master_fd, 65536)
                    if not data:
                        break
                    chunks.append((time.monotonic() - start, data))
                except OSError:
                    break

            # Check child
            try:
                wpid, _ = os.waitpid(child_pid, os.WNOHANG)
                if wpid != 0:
                    # Drain remaining output
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r:
                            break
                        try:
                            d = os.read(master_fd, 65536)
                            if not d:
                                break
                            chunks.append((time.monotonic() - start, d))
                        except OSError:
                            break
                    break
            except ChildProcessError:
                break
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGTERM)
        os.close(master_fd)

    return chunks


def to_asciicast(chunks: list[tuple[float, bytes]], *, width: int, height: int, title: str) -> str:
    header = json.dumps(
        {
            "version": 2,
            "width": width,
            "height": height,
            "title": title,
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        }
    )
    lines = [header]
    for ts, data in chunks:
        lines.append(json.dumps([round(ts, 6), "o", data.decode("utf-8", errors="replace")]))
    return "\n".join(lines) + "\n"


CHILD_SCRIPT = (ROOT / "scripts" / "_record_home_child.py").as_posix()


REQUIRED_MARKERS = (
    "verdict ›",  # noqa: RUF001
    "REACHABLE",
    "CLAIMS VERIFIED",
    "Run orchestrate on this goal?",
)


def sanitize_chunks(chunks: list[tuple[float, bytes]], *, cwd: str) -> list[tuple[float, bytes]]:
    """Never commit host paths: the recording cwd becomes "." and HOME "~".

    A pty read can split a path across two chunks, so replacements run on the
    joined stream. Each chunk then keeps its own timestamp and the matching
    slice of the redacted stream; a replacement that crosses a boundary lands
    in the chunk where the path started.
    """
    home = os.path.expanduser("~")
    swaps = [
        (old.encode(), new.encode())
        for old, new in sorted({(cwd, "."), (home, "~")}, key=lambda kv: len(kv[0]), reverse=True)
        if old and old != "/"
    ]
    joined = b"".join(data for _, data in chunks)
    # Map every byte of the joined stream to the chunk it came from.
    owner: list[int] = []
    for index, (_, data) in enumerate(chunks):
        owner.extend([index] * len(data))
    out_parts: list[bytearray] = [bytearray() for _ in chunks]
    pos = 0
    while pos < len(joined):
        for old, new in swaps:
            if joined.startswith(old, pos):
                out_parts[owner[pos]].extend(new)
                pos += len(old)
                break
        else:
            out_parts[owner[pos]].append(joined[pos])
            pos += 1
    return [(t, bytes(part)) for (t, _), part in zip(chunks, out_parts, strict=True)]


def recording_problems(chunks: list[tuple[float, bytes]]) -> list[str]:
    """Why a capture must not be written as a cast (empty list when it is fine)."""
    text = b"".join(data for _, data in chunks).decode("utf-8", "replace")
    problems = [f"missing {marker!r}" for marker in REQUIRED_MARKERS if marker not in text]
    if "Traceback" in text:
        problems.append("traceback in output")
    return problems


def main() -> int:
    output = ROOT / "docs" / "assets" / "home-prompt.cast"
    python = sys.executable

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT),
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "COLUMNS": str(WIDTH),
        "LINES": str(HEIGHT),
        "PYTHONUNBUFFERED": "1",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "VERDICT_GATEWAY": "http://127.0.0.1:20128",
    }

    inputs = [
        (3.5, "/help\n"),
        (6.0, "/demo\n"),
        (11.0, "/config\n"),
        (14.0, "build a website\n"),
        (16.0, "n\n"),
        (18.0, "\x04"),
    ]

    argv = [python, CHILD_SCRIPT]
    print(f"Recording home prompt demo into {WIDTH}x{HEIGHT} pty", file=sys.stderr)
    chunks = read_pty_with_input(argv, env, inputs, timeout=30.0)
    if not chunks:
        print("No output captured", file=sys.stderr)
        return 1
    # Refuse to write a cast that does not show the session it claims to.
    problems = recording_problems(chunks)
    if problems:
        print(f"Recording incomplete: {problems}", file=sys.stderr)
        return 1

    chunks = sanitize_chunks(chunks, cwd=os.getcwd())
    body = to_asciicast(
        chunks, width=WIDTH, height=HEIGHT, title="Verdict: interactive command prompt"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    duration = chunks[-1][0] if chunks else 0
    print(f"wrote {output} ({len(chunks)} frames, {duration:.1f}s)", file=sys.stderr)

    ts_list = [c[0] for c in chunks]
    first_3s = [t for t in ts_list if t < 3.0]
    if first_3s:
        print(
            f"First 3s: {len(first_3s)} frames, span {first_3s[-1] - first_3s[0]:.2f}s",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

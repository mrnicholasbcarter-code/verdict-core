"""Record the credential-free orchestration demo as an asciinema v2 cast.

Each command runs in a real pseudo-terminal sized to the cast. The typed
prompt is paced for readability. Command output keeps the real PTY read
timestamps and is not split into lines, so escape sequences stay intact.
A failed command, a traceback, or a missing receipt marker rejects the cast.

    python scripts/record_demo.py                       # writes docs/assets/demo.cast
    npx -y svg-term-cli@2.1.1 --in docs/assets/demo.cast \
        --out docs/assets/demo.svg --window --width 110 --height 34

Nothing here reads credentials or calls a network endpoint. The environment
passed to each command is a scrubbed allowlist (PATH, a temporary HOME,
PYTHONPATH, TERM, NO_COLOR).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WIDTH, HEIGHT = 110, 34
TYPE_DELAY = 0.035  # seconds per typed character
PAUSE = 1.2  # pause after each command

TAMPER_DIR = Path(tempfile.gettempdir()) / "verdict-demo-tampered"
# Copy the run, then change the first recorded failure category in the event log.
TAMPER_CMD = (
    f"rm -rf {TAMPER_DIR} && cp -r docs/proof/demo-run {TAMPER_DIR} && "
    f"sed -i '0,/quota_exhausted/s//rate_limited/' {TAMPER_DIR}/events.jsonl"
)


def _commands(python: str) -> list[tuple[str, list[str]]]:
    """(shown command, argv) pairs.

    ``verdict ...`` is shown for ``python -m verdict ...`` (the same CLI entry
    point) so the cast does not depend on an installed console script.
    """
    return [
        (
            "python scripts/demo_orchestrate.py --out docs/proof/demo-run",
            [python, "scripts/demo_orchestrate.py", "--out", "docs/proof/demo-run"],
        ),
        (
            "verdict run-receipt docs/proof/demo-run",
            [python, "-m", "verdict", "run-receipt", "docs/proof/demo-run"],
        ),
        (TAMPER_CMD, ["sh", "-c", TAMPER_CMD]),
        (
            f"verdict run-receipt {TAMPER_DIR}",
            [python, "-m", "verdict", "run-receipt", str(TAMPER_DIR)],
        ),
    ]


def _env(home: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "PYTHONPATH": str(ROOT),
        "TERM": "xterm-256color",
        "NO_COLOR": "1",
        "COLUMNS": str(WIDTH),
        "LINES": str(HEIGHT),
    }


def _capture():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "record_tui_demo", Path(__file__).resolve().parent / "record_tui_demo.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load record_tui_demo")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(out: Path, python: str) -> int:
    """Record each command's real PTY reads. Reject the whole cast on any failure.

    The typed prompt is paced for readability. Command output keeps the real
    read timestamps, shifted onto the cast clock. Reads are not split into
    lines, so an escape sequence stays inside the read that produced it.
    """
    home = tempfile.mkdtemp(prefix="verdict-rec-home-")
    events: list[list[object]] = []
    # No leading idle: the first prompt is written at time 0.
    clock = 0.0
    capture = _capture()
    try:
        for shown, argv in _commands(python):
            events.append([round(clock, 3), "o", "\x1b[1;32m$\x1b[0m "])
            for ch in shown:
                clock += TYPE_DELAY
                events.append([round(clock, 3), "o", ch])
            clock += 0.3
            events.append([round(clock, 3), "o", "\r\n"])
            chunks, code = capture.read_pty_events(argv, _env(home), rows=HEIGHT, cols=WIDTH)
            atomic = capture.coalesce_atomic(chunks)
            raw = b"".join(data for _, data in atomic)
            text = raw.decode("utf-8", errors="replace")
            # The tampered receipt is expected to fail verification (exit 1).
            allowed = code == 0 or (code == 1 and "events_digest mismatch" in text)
            if not allowed:
                raise capture.CaptureError(f"{shown} exited {code}")
            if "Traceback (most recent call last)" in text:
                raise capture.CaptureError(f"traceback while running {shown}")
            if atomic:
                base = clock
                origin = atomic[0][0]
                for at, data in atomic:
                    clock = base + max(0.0, at - origin)
                    events.append([round(clock, 3), "o", data.decode("utf-8", errors="replace")])
            clock += PAUSE
            print(f"recorded: {shown} (exit {code}, {len(atomic)} reads)", file=sys.stderr)
        joined = "".join(str(event[2]) for event in events)
        if "integrity: OK (events digest verified)" not in joined:
            raise capture.CaptureError("demo capture lacks the receipt integrity marker")
        if "events_digest mismatch" not in joined:
            raise capture.CaptureError("demo capture lacks the tampered-receipt mismatch")
    except capture.CaptureError as exc:
        print(f"capture rejected: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(TAMPER_DIR, ignore_errors=True)
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "title": "Verdict orchestration demo (fixture, credential-free, real time)",
        "idle_time_limit": 0.5,
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/sh"},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(header) + "\n")
        for event in events:
            stream.write(json.dumps(event) + "\n")
    print(f"wrote {out} ({len(events)} events, {clock:.1f}s)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", default=str(ROOT / "docs" / "assets" / "demo.cast"))
    parser.add_argument("--python", default=sys.executable, help="Interpreter for the demo")
    args = parser.parse_args(argv)
    return record(Path(args.out), args.python)


if __name__ == "__main__":
    raise SystemExit(main())

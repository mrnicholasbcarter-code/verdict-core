"""Record the credential-free orchestration demo as an asciinema v2 cast.

Stdlib only. Each command runs in a real pseudo-terminal. The recorder takes
its output bytes verbatim and paces them line by line, so the cast plays at a
readable speed. Only the timing is synthetic. The content is the real output.

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
import pty
import select
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 110, 34
TYPE_DELAY = 0.035  # seconds per typed character
LINE_DELAY = 0.09  # seconds per output line
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


def _run_in_pty(argv: list[str], env: dict[str, str]) -> tuple[bytes, int]:
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


def record(out: Path, python: str) -> int:
    home = tempfile.mkdtemp(prefix="verdict-rec-home-")
    events: list[list[object]] = []
    clock = 0.5
    try:
        for shown, argv in _commands(python):
            events.append([round(clock, 3), "o", "\x1b[1;32m$\x1b[0m "])
            for ch in shown:
                clock += TYPE_DELAY
                events.append([round(clock, 3), "o", ch])
            clock += 0.3
            events.append([round(clock, 3), "o", "\r\n"])
            output, code = _run_in_pty(argv, _env(home))
            text = output.decode("utf-8", errors="replace")
            for line in text.splitlines(keepends=True):
                clock += LINE_DELAY
                events.append([round(clock, 3), "o", line])
            clock += PAUSE
            print(f"recorded: {shown} (exit {code})", file=sys.stderr)
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(TAMPER_DIR, ignore_errors=True)
    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "title": "Verdict orchestration demo (fixture, credential-free)",
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
